import os
os.environ['PYTHONHASHSEED'] = '0'
import settings
import helpers
import glob
import random
import pandas
import ntpath
import numpy
from keras.optimizers import Adam, SGD
from keras.layers import Input, Convolution2D, MaxPooling2D, UpSampling2D, merge, Conv3D, MaxPooling3D, UpSampling3D, LeakyReLU, BatchNormalization, Flatten, Dense, Dropout, ZeroPadding3D, AveragePooling3D, Activation
from keras.models import Model, load_model, model_from_json
from keras.metrics import binary_accuracy, binary_crossentropy, mean_squared_error, mean_absolute_error
from keras import backend as K
from keras.callbacks import ModelCheckpoint, Callback, LearningRateScheduler, History, TensorBoard
from keras.constraints import maxnorm
import shutil
from timeprofile import calltimeprofile, print_prof_data

# The below is necessary for starting Numpy generated random numbers
# in a well-defined initial state.
numpy.random.seed(42)

# The below is necessary for starting core Python generated random numbers
# in a well-defined state.
random.seed(2)

# limit memory usage..
import tensorflow as tf
# The below tf.set_random_seed() will make random number generation
# in the TensorFlow backend have a well-defined initial state.
# For further details, see: https://www.tensorflow.org/api_docs/python/tf/set_random_seed
tf.set_random_seed(1234)
from keras.backend.tensorflow_backend import set_session
config = tf.ConfigProto()
config.gpu_options.per_process_gpu_memory_fraction = 0.5
set_session(tf.Session(config=config))

logger = helpers.getlogger(os.path.splitext(os.path.basename(__file__))[0])
# zonder aug, 10:1 99 train, 97 test, 0.27 cross entropy, before commit 573
# 3 pools istead of 4 gives (bigger end layer) gives much worse validation accuray + logloss .. strange ?
# 32 x 32 x 32 lijkt het beter te doen dan 48 x 48 x 48..

K.set_image_dim_ordering("tf")
CUBE_SIZE = 32
MEAN_PIXEL_VALUE = settings.MEAN_PIXEL_VALUE_NODULE
POS_WEIGHT = 2
NEGS_PER_POS = 1   # ============== NEGS_PER_POS = 20
P_TH = 0.6
# POS_IMG_DIR = "luna16_train_cubes_pos"
LEARN_RATE = 0.001

USE_DROPOUT = True

TENSORBOARD_LOG_DIR = "tfb_log/"

class LossHistory(Callback):
    def on_train_begin(self, logs={}):
        self.losses = []

    def on_batch_end(self, batch, logs={}):
        self.losses.append(logs.get('loss'))


def prepare_image_for_net3D(img):
    img = img.astype(numpy.float32)
    img -= MEAN_PIXEL_VALUE
    img /= 255.
    img = img.reshape(1, img.shape[0], img.shape[1], img.shape[2], 1)
    return img


def get_train_holdout_files(fold_count, train_percentage=80, logreg=True, ndsb3_holdout=0, manual_labels=True, full_luna_set=False, local_patient_set=False):
    logger.info("Get train/holdout files.")
    # pos_samples = glob.glob(settings.BASE_DIR_SSD + "luna16_train_cubes_pos/*.png")
    pos_samples = glob.glob(settings.WORKING_DIR + "generated_traindata/luna16_train_cubes_lidc/*.png")
    logger.info("Pos samples: {0}".format(len(pos_samples)))

    pos_samples_manual = glob.glob(settings.WORKING_DIR + "generated_traindata/luna16_train_cubes_manual/*_pos.png")
    logger.info("Pos samples manual: {0}".format(len(pos_samples_manual)))
    pos_samples += pos_samples_manual

    random.shuffle(pos_samples)
    train_pos_count = int((len(pos_samples) * train_percentage) / 100)
    pos_samples_train = pos_samples[:train_pos_count]
    pos_samples_holdout = pos_samples[train_pos_count:]
    if full_luna_set:
        pos_samples_train += pos_samples_holdout
        if manual_labels:
            pos_samples_holdout = []


    ndsb3_list = glob.glob(settings.WORKING_DIR+ "generated_traindata/ndsb3_train_cubes_manual/*.png")
    logger.info("Ndsb3 samples: {0} ".format(len(ndsb3_list)))

    pos_samples_ndsb3_fold = []
    pos_samples_ndsb3_holdout = []
    ndsb3_pos = 0
    ndsb3_neg = 0
    ndsb3_pos_holdout = 0
    ndsb3_neg_holdout = 0
    if manual_labels:
        for file_path in ndsb3_list:
            file_name = ntpath.basename(file_path)

            parts = file_name.split("_")
            if int(parts[4]) == 0 and parts[3] != "neg":  # skip positive non-cancer-cases
                continue

            if fold_count == 3:
                if parts[3] == "neg":  # skip negative cases
                    continue


            patient_id = parts[1]
            patient_fold = helpers.get_patient_fold(patient_id) % fold_count
            if patient_fold == ndsb3_holdout:
                logger.info("In holdout: {0}".format(patient_id))
                pos_samples_ndsb3_holdout.append(file_path)
                if parts[3] == "neg":
                    ndsb3_neg_holdout += 1
                else:
                    ndsb3_pos_holdout += 1
            else:
                pos_samples_ndsb3_fold.append(file_path)
                logger.info("In fold: {0}".format(patient_id))
                if parts[3] == "neg":
                    ndsb3_neg += 1
                else:
                    ndsb3_pos += 1

    logger.info("{0} ndsb3 pos labels train".format(ndsb3_pos))
    logger.info("{0} ndsb3 neg labels train".format(ndsb3_neg))
    logger.info("{0} ndsb3 pos labels holdout".format(ndsb3_pos_holdout))
    logger.info("{0} ndsb3 neg labels holdout".format(ndsb3_neg_holdout))


    pos_samples_hospital_train=[]
    pos_samples_hospital_holdout=[]
    if local_patient_set:
        logger.info("Including hospital cases...")
        hospital_list = glob.glob(settings.WORKING_DIR+ "generated_traindata/hospital_train_cubes_manual/*.png")
        random.shuffle(hospital_list)
        train_hospital_count = int((len(hospital_list) * train_percentage) / 100)
        pos_samples_hospital_train = hospital_list[:train_hospital_count]
        pos_samples_hospital_holdout = hospital_list[train_hospital_count:]

    if manual_labels:
        for times_ndsb3 in range(4):  # make ndsb labels count 4 times just like in LIDC when 4 doctors annotated a nodule
            pos_samples_train += pos_samples_ndsb3_fold
            pos_samples_holdout += pos_samples_ndsb3_holdout

    neg_samples_edge = glob.glob(settings.WORKING_DIR + "generated_traindata/luna16_train_cubes_auto/*_edge.png")
    logger.info("Edge samples: {0}".format(len(neg_samples_edge)))

    # neg_samples_white = glob.glob(settings.BASE_DIR_SSD + "luna16_train_cubes_auto/*_white.png")
    neg_samples_luna = glob.glob(settings.WORKING_DIR + "generated_traindata/luna16_train_cubes_auto/*_luna.png")
    logger.info("Luna samples: {0}".format(len(neg_samples_luna)))

    # neg_samples = neg_samples_edge + neg_samples_white
    neg_samples = neg_samples_edge + neg_samples_luna
    random.shuffle(neg_samples)

    train_neg_count = int((len(neg_samples) * train_percentage) / 100)

    neg_samples_falsepos = []
    for file_path in glob.glob(settings.WORKING_DIR + "generated_traindata/luna16_train_cubes_auto/*_falsepos.png"):
        neg_samples_falsepos.append(file_path)
    logger.info("Falsepos LUNA count: {0}".format(len(neg_samples_falsepos)))

    neg_samples_train = neg_samples[:train_neg_count]
    neg_samples_train += neg_samples_falsepos + neg_samples_falsepos + neg_samples_falsepos
    neg_samples_holdout = neg_samples[train_neg_count:]
    if full_luna_set:
        neg_samples_train += neg_samples_holdout

    train_res = []
    holdout_res = []
    logger.info("Train positive samples: {0}".format(len(pos_samples_train)))
    logger.info("Train negative samples: {0}".format(len(neg_samples_train)))
    logger.info("Train hospital samples: {0}".format(len(pos_samples_hospital_train)))
    logger.info("Holdout positive samples: {0}".format(len(pos_samples_holdout)))
    logger.info("Holdout negative samples: {0}".format(len(neg_samples_holdout)))
    logger.info("Holdout hospital samples: {0}".format(len(pos_samples_hospital_holdout)))
    sets = [(train_res, pos_samples_train, neg_samples_train, pos_samples_hospital_train),
            (holdout_res, pos_samples_holdout, neg_samples_holdout, pos_samples_hospital_holdout)]
    for set_item in sets:
        pos_idx = 0
        negs_per_pos = NEGS_PER_POS
        res = set_item[0]
        neg_samples = set_item[2]
        pos_samples = set_item[1]
        hospital_samples = set_item[3]
        logger.info("Pos: {0}".format(len(pos_samples)))
        ndsb3_pos = 0
        ndsb3_neg = 0
        for index, neg_sample_path in enumerate(neg_samples):
            # res.append(sample_path + "/")
            res.append((neg_sample_path, 0, 0))
            if index % negs_per_pos == 0:
                pos_sample_path = pos_samples[pos_idx]
                file_name = ntpath.basename(pos_sample_path)
                parts = file_name.split("_")
                if parts[0].startswith("ndsb3manual"):
                    if parts[3] == "pos":
                        class_label = 1  # only take positive examples where we know there was a cancer..
                        cancer_label = int(parts[4])
                        assert cancer_label == 1
                        size_label = int(parts[5])
                        # logger.info(parts[1], size_label)
                        assert class_label == 1
                        if size_label < 1:
                            logger.info("{0} nodule size < 1".format(pos_sample_path))
                        assert size_label >= 1
                        ndsb3_pos += 1
                    else:
                        class_label = 0
                        size_label = 0
                        ndsb3_neg += 1
                else:
                    class_label = int(parts[-2])
                    size_label = int(parts[-3])
                    assert class_label == 1
                    assert parts[-1] == "pos.png"
                    assert size_label >= 1

                res.append((pos_sample_path, class_label, size_label))
                pos_idx += 1
                pos_idx %= len(pos_samples)
                # ===================不重复取pos samples
                # if pos_idx % len(pos_samples) == 0:
                #    break

        if local_patient_set:
            for index, hospital_sample_path in enumerate(hospital_samples):
                file_name = os.path.basename(hospital_sample_path)
                parts = file_name.split("_")
                if parts[3] == "pos":
                    class_label = 1
                else:
                    class_label = 0
                size_label = int(parts[5])
                if size_label < 1:
                    logger.info("{0} nodule size < 1".format(file_name))
                logger.info("Add sample {0} class: {1} size: {2}".format(hospital_sample_path, class_label, size_label))
                res.append((hospital_sample_path, class_label, size_label))

        logger.info("ndsb3 pos: {0}".format(ndsb3_pos))
        logger.info("ndsb3 neg: {0}".format(ndsb3_neg))

    logger.info("Train count: {0}, holdout count: {1} ".format(len(train_res), len(holdout_res)))
    return train_res, holdout_res


def data_generator(batch_size, record_list, train_set):
    batch_idx = 0
    means = []
    random_state = numpy.random.RandomState(1301)
    while True:
        img_list = []
        class_list = []
        size_list = []
        if train_set:
            random.shuffle(record_list)
        CROP_SIZE = CUBE_SIZE
        # CROP_SIZE = 48
        for record_idx, record_item in enumerate(record_list):
            #rint patient_dir
            class_label = record_item[1]
            size_label = record_item[2]
            if class_label == 0:
                cube_image = helpers.load_cube_img(record_item[0], 6, 8, 48)
                # if train_set:
                #     # helpers.save_cube_img("c:/tmp/pre.png", cube_image, 8, 8)
                #     cube_image = random_rotate_cube_img(cube_image, 0.99, -180, 180)
                #
                # if train_set:
                #     if random.randint(0, 100) > 0.1:
                #         # cube_image = numpy.flipud(cube_image)
                #         cube_image = elastic_transform48(cube_image, 64, 8, random_state)
                wiggle = 48 - CROP_SIZE - 1
                indent_x = 0
                indent_y = 0
                indent_z = 0
                if wiggle > 0:
                    indent_x = random.randint(0, wiggle)
                    indent_y = random.randint(0, wiggle)
                    indent_z = random.randint(0, wiggle)
                cube_image = cube_image[indent_z:indent_z + CROP_SIZE, indent_y:indent_y + CROP_SIZE, indent_x:indent_x + CROP_SIZE]

                if train_set:
                    if random.randint(0, 100) > 50:
                        cube_image = numpy.fliplr(cube_image)
                    if random.randint(0, 100) > 50:
                        cube_image = numpy.flipud(cube_image)
                    if random.randint(0, 100) > 50:
                        cube_image = cube_image[:, :, ::-1]
                    if random.randint(0, 100) > 50:
                        cube_image = cube_image[:, ::-1, :]

                if CROP_SIZE != CUBE_SIZE:
                    cube_image = helpers.rescale_patient_images2(cube_image, (CUBE_SIZE, CUBE_SIZE, CUBE_SIZE))
                assert cube_image.shape == (CUBE_SIZE, CUBE_SIZE, CUBE_SIZE)
            else:
                cube_image = helpers.load_cube_img(record_item[0], 8, 8, 64)

                if train_set:
                    pass

                current_cube_size = cube_image.shape[0]
                indent_x = (current_cube_size - CROP_SIZE) / 2
                indent_y = (current_cube_size - CROP_SIZE) / 2
                indent_z = (current_cube_size - CROP_SIZE) / 2
                wiggle_indent = 0
                wiggle = current_cube_size - CROP_SIZE - 1
                if wiggle > (CROP_SIZE / 2):
                    wiggle_indent = CROP_SIZE / 4
                    wiggle = current_cube_size - CROP_SIZE - CROP_SIZE / 2 - 1
                if train_set:
                    indent_x = wiggle_indent + random.randint(0, wiggle)
                    indent_y = wiggle_indent + random.randint(0, wiggle)
                    indent_z = wiggle_indent + random.randint(0, wiggle)

                indent_x = int(indent_x)
                indent_y = int(indent_y)
                indent_z = int(indent_z)
                cube_image = cube_image[indent_z:indent_z + CROP_SIZE, indent_y:indent_y + CROP_SIZE, indent_x:indent_x + CROP_SIZE]
                if CROP_SIZE != CUBE_SIZE:
                    cube_image = helpers.rescale_patient_images2(cube_image, (CUBE_SIZE, CUBE_SIZE, CUBE_SIZE))
                assert cube_image.shape == (CUBE_SIZE, CUBE_SIZE, CUBE_SIZE)

                if train_set:
                    if random.randint(0, 100) > 50:
                        cube_image = numpy.fliplr(cube_image)
                    if random.randint(0, 100) > 50:
                        cube_image = numpy.flipud(cube_image)
                    if random.randint(0, 100) > 50:
                        cube_image = cube_image[:, :, ::-1]
                    if random.randint(0, 100) > 50:
                        cube_image = cube_image[:, ::-1, :]


            means.append(cube_image.mean())
            img3d = prepare_image_for_net3D(cube_image)
            if train_set:
                if len(means) % 1000000 == 0:
                    logger.info("Mean: {0}".format(sum(means) / len(means)))
            img_list.append(img3d)
            class_list.append(class_label)
            size_list.append(size_label)

            batch_idx += 1
            if batch_idx >= batch_size:
                x = numpy.vstack(img_list)
                y_class = numpy.vstack(class_list)
                y_size = numpy.vstack(size_list)
                yield x, {"out_class": y_class, "out_malignancy": y_size}
                img_list = []
                class_list = []
                size_list = []
                batch_idx = 0


def writemodelsummary(s):
    with open('./modelsummary.txt','w+') as f:
        f.write(s)
        print(s)


def get_net(input_shape=(CUBE_SIZE, CUBE_SIZE, CUBE_SIZE, 1), load_weight_path=None, features=False, mal=False) -> Model:
    inputs = Input(shape=input_shape, name="input_1")
    x = inputs
    if USE_DROPOUT:
        x = Dropout(rate=0.1)(x)
    #x = AveragePooling3D(pool_size=(2, 1, 1), strides=(2, 1, 1), border_mode="same")(x)
    x = AveragePooling3D(pool_size=(2, 1, 1), strides=(2, 1, 1), padding="same")(x)
    #x = Convolution3D(64, 3, 3, 3, activation='relu', border_mode='same', name='conv1', subsample=(1, 1, 1))(x)
    x = Conv3D(64, (3, 3, 3), strides=(1, 1, 1), activation='relu', padding='same', kernel_initializer='lecun_normal', kernel_constraint=maxnorm(4), name='conv1')(x)
    #x = MaxPooling3D(pool_size=(1, 2, 2), strides=(1, 2, 2), border_mode='valid', name='pool1')(x)
    x = MaxPooling3D(pool_size=(1, 2, 2), strides=(1, 2, 2), padding='valid', name='pool1')(x)
    if USE_DROPOUT:
        x = Dropout(rate=0.25)(x)

    # 2nd layer group
    #x = Convolution3D(128, 3, 3, 3, activation='relu', border_mode='same', name='conv2', subsample=(1, 1, 1))(x)
    x = Conv3D(128, (3, 3, 3), strides=(1, 1, 1), activation='relu', padding='same', kernel_initializer='lecun_normal', kernel_constraint=maxnorm(4), name='conv2')(x)
    #x = MaxPooling3D(pool_size=(2, 2, 2), strides=(2, 2, 2), border_mode='valid', name='pool2')(x)
    x = MaxPooling3D(pool_size=(2, 2, 2), strides=(2, 2, 2), padding='valid', name='pool2')(x)
    if USE_DROPOUT:
        x = Dropout(rate=0.25)(x)

    # 3rd layer group
    #x = Convolution3D(256, 3, 3, 3, activation='relu', border_mode='same', name='conv3a', subsample=(1, 1, 1))(x)
    x = Conv3D(256, (3, 3, 3), strides=(1, 1, 1), activation='relu', padding='same', kernel_initializer='lecun_normal', kernel_constraint=maxnorm(4), name='conv3a')(x)
    #x = Convolution3D(256, 3, 3, 3, activation='relu', border_mode='same', name='conv3b', subsample=(1, 1, 1))(x)
    x = Conv3D(256, (3, 3, 3), strides=(1, 1, 1), activation='relu', padding='same', kernel_initializer='lecun_normal', kernel_constraint=maxnorm(4), name='conv3b')(x)
    #x = MaxPooling3D(pool_size=(2, 2, 2), strides=(2, 2, 2), border_mode='valid', name='pool3')(x)
    x = MaxPooling3D(pool_size=(2, 2, 2), strides=(2, 2, 2), padding='valid', name='pool3')(x)
    if USE_DROPOUT:
        x = Dropout(rate=0.5)(x)

    # 4th layer group
    #x = Convolution3D(512, 3, 3, 3, activation='relu', border_mode='same', name='conv4a', subsample=(1, 1, 1))(x)
    x = Conv3D(512, (3, 3, 3), strides=(1, 1, 1), activation='relu', padding='same', kernel_initializer='lecun_normal', kernel_constraint=maxnorm(4), name='conv4a')(x)
    #x = Convolution3D(512, 3, 3, 3, activation='relu', border_mode='same', name='conv4b', subsample=(1, 1, 1),)(x)
    x = Conv3D(512, (3, 3, 3), strides=(1, 1, 1), activation='relu', padding='same', kernel_initializer='lecun_normal', kernel_constraint=maxnorm(4), name='conv4b')(x)
    #x = MaxPooling3D(pool_size=(2, 2, 2), strides=(2, 2, 2), border_mode='valid', name='pool4')(x)
    x = MaxPooling3D(pool_size=(2, 2, 2), strides=(2, 2, 2), padding='valid', name='pool4')(x)
    if USE_DROPOUT:
        x = Dropout(rate=0.5)(x)

    last64 = Conv3D(64, (2, 2, 2), kernel_initializer='lecun_normal', activation="relu", name="last_64")(x)
    out_class = Conv3D(1, (1, 1, 1), kernel_initializer='lecun_normal', activation="sigmoid", kernel_constraint=maxnorm(4), name="out_class_last")(last64)
    out_class = Flatten(name="out_class")(out_class)

    out_malignancy = Conv3D(1, (1, 1, 1), kernel_initializer='lecun_normal', activation=None, kernel_constraint=maxnorm(4), name="out_malignancy_last")(last64)
    out_malignancy = Flatten(name="out_malignancy")(out_malignancy)

    model = Model(inputs=inputs, outputs=[out_class, out_malignancy])
    if load_weight_path is not None:
        model.load_weights(load_weight_path, by_name=False)

    MOMENTUM = 0.9
    NESTEROV = True
    if USE_DROPOUT:
        MOMENTUM = 0.95
        NESTEROV = False

    model.compile(optimizer=SGD(lr=LEARN_RATE, momentum=MOMENTUM, nesterov=NESTEROV), loss={"out_class": "binary_crossentropy", "out_malignancy": mean_absolute_error}, metrics={"out_class": [binary_accuracy, binary_crossentropy], "out_malignancy": mean_absolute_error})

    if features:
        model = Model(input=inputs, output=[last64])
    # model.summary(line_length=140)
    model.summary(print_fn=writemodelsummary)
    return model


def step_decay(epoch):
    res = 0.001
    if epoch > 5:
        res = 0.0001
    logger.info("learnrate: {0} epoch: {1}".format(res, epoch))
    return res


class LoggingCallback(Callback):
    """Callback that logs message at end of epoch.
    """
    def __init__(self, print_fcn=print):
        Callback.__init__(self)
        self.print_fcn = print_fcn

    def on_epoch_end(self, epoch, logs={}):

        msg = "{Epoch: %i} %s" % (epoch, ", ".join("%s: %f" % (k, v) for k, v in logs.items()))
        self.print_fcn(msg)


# @calltimeprofile(logger)
def train(model_name, fold_count, train_full_set=False, load_weights_path=None, ndsb3_holdout=0, manual_labels=True, local_patient_set=False):
    batch_size = 16
    train_files, holdout_files = get_train_holdout_files(train_percentage=80, ndsb3_holdout=ndsb3_holdout, manual_labels=manual_labels, full_luna_set=train_full_set, fold_count=fold_count,local_patient_set=local_patient_set)

    # train_files = train_files[:100]
    # holdout_files = train_files[:10]
    train_gen = data_generator(batch_size, train_files, True)
    holdout_gen = data_generator(batch_size, holdout_files, False)
    for i in range(0, 10):
        tmp = next(holdout_gen)
        cube_img = tmp[0][0].reshape(CUBE_SIZE, CUBE_SIZE, CUBE_SIZE, 1)
        cube_img = cube_img[:, :, :, 0]
        cube_img *= 255.
        cube_img += MEAN_PIXEL_VALUE
        # helpers.save_cube_img("c:/tmp/img_" + str(i) + ".png", cube_img, 4, 8)
        # logger.info(tmp)

    #input("Enter any key to continue...")
    history = LossHistory()
    logcallback = LoggingCallback(logger.info)

    learnrate_scheduler = LearningRateScheduler(step_decay)
    model = get_net(load_weight_path=load_weights_path)

    # Tensorboard setting
    if not os.path.exists(TENSORBOARD_LOG_DIR):
        os.makedirs(TENSORBOARD_LOG_DIR)

    tensorboard_callback = TensorBoard(
        log_dir=TENSORBOARD_LOG_DIR,
        histogram_freq=2,
        # write_images=True, # Enabling this line would require more than 5 GB at each `histogram_freq` epoch.
        write_graph=True
        # embeddings_freq=3,
        # embeddings_layer_names=list(embeddings_metadata.keys()),
        # embeddings_metadata=embeddings_metadata
    )
    tensorboard_callback.set_model(model)

    holdout_txt = "_h" + str(ndsb3_holdout) if manual_labels else ""
    if train_full_set:
        holdout_txt = "_fs" + holdout_txt
    checkpoint = ModelCheckpoint(settings.WORKING_DIR + "workdir/model_" + model_name + "_" + holdout_txt + "_e" + "{epoch:02d}-{val_loss:.4f}.hd5", monitor='val_loss', verbose=1, save_best_only=not train_full_set, save_weights_only=False, mode='auto', period=1)
    checkpoint_fixed_name = ModelCheckpoint(settings.WORKING_DIR + "workdir/model_" + model_name + "_" + holdout_txt + "_best.hd5", monitor='val_loss', verbose=1, save_best_only=True, save_weights_only=False, mode='auto', period=1)
    # train_history = model.fit_generator(train_gen, len(train_files) / 1, 12, validation_data=holdout_gen, nb_val_samples=len(holdout_files) / 1, callbacks=[checkpoint, checkpoint_fixed_name, learnrate_scheduler])
    train_history = model.fit_generator(train_gen, len(train_files) / batch_size, 1, validation_data=holdout_gen,
                                        validation_steps=len(holdout_files) / batch_size,
                                        callbacks=[logcallback, tensorboard_callback, checkpoint_fixed_name, learnrate_scheduler])
    logger.info("Model fit_generator finished.")
    model.save(settings.WORKING_DIR + "workdir/model_" + model_name + "_" + holdout_txt + "_end.hd5")
    
    logger.info("history keys: {0}".format(train_history.history.keys()))

    # numpy_loss_history = numpy.array(history.history)
    # numpy.savetxt("workdir/model_" + model_name + "_" + holdout_txt + "_loss_history.txt", numpy_loss_history, delimiter=",")
    pandas.DataFrame(train_history.history).to_csv(settings.WORKING_DIR + "workdir/model_" + model_name + "_" + holdout_txt + "history.csv")

if __name__ == "__main__":
    if not os.path.exists(settings.WORKING_DIR + "models/"):
        os.mkdir(settings.WORKING_DIR + "models")
    if not os.path.exists(settings.WORKING_DIR + "workdir/"):
        os.mkdir(settings.WORKING_DIR + "workdir")

    try:    
        if True:
            # model 1 on luna16 annotations. full set 1 versions for blending
            #logger.info("Train the full luna set without manual labels...")
            train(train_full_set=True, load_weights_path=None, model_name="luna16_full", fold_count=-1, manual_labels=False,local_patient_set=False)
            shutil.copy(settings.WORKING_DIR + "workdir/model_luna16_full__fs_best.hd5", settings.WORKING_DIR + "models/model_luna16_full__fs_best.hd5")

        # model 2 on luna16 annotations + ndsb pos annotations. 3 folds (1st half, 2nd half of ndsb patients) 2 versions for blending
        # if True:
        #     logger.info("Train the full luna set with manual labels and ndsb3_holdout=0 v1...")
        #     train(train_full_set=True, load_weights_path=None, ndsb3_holdout=0, manual_labels=True, model_name="luna_posnegndsb_v1", fold_count=2,local_patient_set=False)
        #     logger.info("Train the full luna set with manual labels and ndsb3_holdout=1 v1...")
        #     train(train_full_set=True, load_weights_path=None, ndsb3_holdout=1, manual_labels=True, model_name="luna_posnegndsb_v1", fold_count=2,local_patient_set=False)
        #     shutil.copy(settings.WORKING_DIR + "workdir/model_luna_posnegndsb_v1__fs_h0_end.hd5", settings.WORKING_DIR + "models/model_luna_posnegndsb_v1__fs_h0_end.hd5")
        #     shutil.copy(settings.WORKING_DIR + "workdir/model_luna_posnegndsb_v1__fs_h1_end.hd5", settings.WORKING_DIR + "models/model_luna_posnegndsb_v1__fs_h1_end.hd5")
        #
        # if True:
        #     logger.info("Train the full luna set with manual labels and ndsb3_holdout=0 v2...")
        #     train(train_full_set=True, load_weights_path=None, ndsb3_holdout=0, manual_labels=True, model_name="luna_posnegndsb_v2", fold_count=2,local_patient_set=False)
        #     logger.info("Train the full luna set with manual labels and ndsb3_holdout=1 v2...")
        #     train(train_full_set=True, load_weights_path=None, ndsb3_holdout=1, manual_labels=True, model_name="luna_posnegndsb_v2", fold_count=2,local_patient_set=False)
        #     shutil.copy(settings.WORKING_DIR + "workdir/model_luna_posnegndsb_v2__fs_h0_end.hd5", settings.WORKING_DIR + "models/model_luna_posnegndsb_v2__fs_h0_end.hd5")
        #     shutil.copy(settings.WORKING_DIR + "workdir/model_luna_posnegndsb_v2__fs_h1_end.hd5", settings.WORKING_DIR + "models/model_luna_posnegndsb_v2__fs_h1_end.hd5")
    except Exception as ex:
        helpers.cleanlogger(logger)
    finally:
        logger.handlers.clear()


