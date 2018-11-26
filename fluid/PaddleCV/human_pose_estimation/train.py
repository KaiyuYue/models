# Copyright (c) 2018-present, Baidu, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##############################################################################

"""Functions for training."""

import os
import numpy as np
import cv2
import paddle
import paddle.fluid as fluid
import paddle.fluid.layers as layers
import argparse
import functools

from lib import pose_resnet
from utils.utility import add_arguments, print_arguments

parser = argparse.ArgumentParser(description=__doc__)
add_arg = functools.partial(add_arguments, argparser=parser)
# yapf: disable
add_arg('batch_size',       int,   32,                   "Minibatch size.")
add_arg('dataset',          str,   'mpii',               "Dataset")
add_arg('use_gpu',          bool,  True,                 "Whether to use GPU or not.")
add_arg('num_epochs',       int,   140,                  "Number of epochs.")
add_arg('total_images',     int,   144406,               "Training image number.")
add_arg('kp_dim',           int,   16,                   "Class number.")
add_arg('model_save_dir',   str,   "output",             "Model save directory")
add_arg('with_mem_opt',     bool,  True,                 "Whether to use memory optimization or not.")
add_arg('pretrained_model', str,   None,                 "Whether to use pretrained model.")
add_arg('checkpoint',       str,   None,                 "Whether to resume checkpoint.")
add_arg('lr',               float, 0.001,                "Set learning rate.")
add_arg('lr_strategy',      str,   "piecewise_decay",    "Set the learning rate decay strategy.")
# yapf: enable

def optimizer_setting(args, params):
    lr_drop_ratio = 0.1

    ls = params["learning_strategy"]

    if ls["name"] == "piecewise_decay":
        total_images = params["total_images"]
        batch_size = ls["batch_size"]
        step = int(total_images / batch_size + 1)

        ls['epochs'] = [91, 121]
        print('=> LR will be dropped at the epoch of {}'.format(ls['epochs']))

        bd = [step * e for e in ls["epochs"]]
        base_lr = params["lr"]
        lr = []
        lr = [base_lr * (lr_drop_ratio**i) for i in range(len(bd) + 1)]

        # AdamOptimizer
        optimizer = paddle.fluid.optimizer.AdamOptimizer(
                        learning_rate=fluid.layers.piecewise_decay(
                        boundaries=bd, values=lr))
    else:
        lr = params["lr"]
        optimizer = fluid.optimizer.Momentum(
            learning_rate=lr,
            momentum=0.9,
            regularization=fluid.regularizer.L2Decay(0.0005))

    return optimizer

def train(args):
    if args.dataset == 'coco':
        import lib.coco_reader as reader
        IMAGE_SIZE = [288, 384]
        HEATMAP_SIZE = [72, 96]
        args.kp_dim = 17
        args.total_images = 144406 # 149813
    elif args.dataset == 'mpii':
        import lib.mpii_reader as reader
        IMAGE_SIZE = [384, 384]
        HEATMAP_SIZE = [96, 96]
        args.kp_dim = 16
        args.total_images = 22246
    else:
        raise ValueError('The dataset {} is not supported yet.'.format(args.dataset))

    print_arguments(args)

    # image and target
    image = layers.data(name='image', shape=[3, IMAGE_SIZE[1], IMAGE_SIZE[0]], dtype='float32')
    target = layers.data(name='target', shape=[args.kp_dim, HEATMAP_SIZE[1], HEATMAP_SIZE[0]], dtype='float32')
    target_weight = layers.data(name='target_weight', shape=[args.kp_dim, 1], dtype='float32')

    # build model
    model = pose_resnet.ResNet(layers=50, kps_num=args.kp_dim)

    # output
    loss, output = model.net(input=image, target=target, target_weight=target_weight)

    # parameters from model and arguments
    params = {}
    params["total_images"] = args.total_images
    params["lr"] = args.lr
    params["num_epochs"] = args.num_epochs
    params["learning_strategy"] = {}
    params["learning_strategy"]["batch_size"] = args.batch_size
    params["learning_strategy"]["name"] = args.lr_strategy

    # initialize optimizer
    optimizer = optimizer_setting(args, params)
    optimizer.minimize(loss)

    if args.with_mem_opt:
        fluid.memory_optimize(fluid.default_main_program(),
                              skip_opt_set=[loss.name, output.name, target.name])

    place = fluid.CUDAPlace(0) if args.use_gpu else fluid.CPUPlace()
    exe = fluid.Executor(place)
    exe.run(fluid.default_startup_program())

    args.pretrained_model = './pretrained/resnet_50/115'
    if args.pretrained_model:
        def if_exist(var):
            exist_flag = os.path.exists(os.path.join(args.pretrained_model, var.name))
            return exist_flag
        fluid.io.load_vars(exe, args.pretrained_model, predicate=if_exist)

    if args.checkpoint is not None:
        fluid.io.load_persistables(exe, args.checkpoint)

    # dataloader
    train_reader = paddle.batch(reader.train(), batch_size=args.batch_size)
    feeder = fluid.DataFeeder(place=place, feed_list=[image, target, target_weight])

    train_exe = fluid.ParallelExecutor(
        use_cuda=True if args.use_gpu else False, loss_name=loss.name)
    fetch_list = [image.name, loss.name, output.name]

    for pass_id in range(params["num_epochs"]):
        for batch_id, data in enumerate(train_reader()):
            current_lr = np.array(paddle.fluid.global_scope().find_var('learning_rate').get_tensor())

            input_image, loss, out_heatmaps = train_exe.run(
                    fetch_list, feed=feeder.feed(data))

            loss = np.mean(np.array(loss))

            print('Epoch [{:4d}/{:3d}] LR: {:.10f} '
                  'Loss = {:.5f}'.format(
                  batch_id, pass_id, current_lr[0], loss))

            if batch_id % 10 == 0:
                save_batch_heatmaps(input_image, out_heatmaps, file_name='visualization@train.jpg', normalize=True)

        model_path = os.path.join(args.model_save_dir + '/' + 'simplebase-{}'.format(args.dataset),
                                  str(pass_id))
        if not os.path.isdir(model_path):
            os.makedirs(model_path)
        fluid.io.save_persistables(exe, model_path)

def save_batch_heatmaps(batch_image, batch_heatmaps, file_name, normalize=True):
    """
    batch_image: [batch_size, channel, height, width]
    batch_heatmaps: ['batch_size, num_joints, height, width]
    file_name: saved file name
    """
    if normalize:
        min = np.array(batch_image.min(), dtype=np.float)
        max = np.array(batch_image.max(), dtype=np.float)

        batch_image = np.add(batch_image, -min)
        batch_image = np.divide(batch_image, max - min + 1e-5)

    batch_size, num_joints, \
            heatmap_height, heatmap_width = batch_heatmaps.shape

    grid_image = np.zeros((batch_size*heatmap_height,
                           (num_joints+1)*heatmap_width,
                           3),
                          dtype=np.uint8)

    preds, maxvals = get_max_preds(batch_heatmaps)

    for i in range(batch_size):
        image = batch_image[i] * 255
        image = image.clip(0, 255).astype(np.uint8)
        image = image.transpose(1, 2, 0)

        heatmaps = batch_heatmaps[i] * 255
        heatmaps = heatmaps.clip(0, 255).astype(np.uint8)

        resized_image = cv2.resize(image,
                                   (int(heatmap_width), int(heatmap_height)))
        height_begin = heatmap_height * i
        height_end = heatmap_height * (i + 1)
        for j in range(num_joints):
            cv2.circle(resized_image,
                       (int(preds[i][j][0]), int(preds[i][j][1])),
                       1, [0, 0, 255], 1)
            heatmap = heatmaps[j, :, :]
            colored_heatmap = cv2.applyColorMap(heatmap, cv2.COLORMAP_JET)
            masked_image = colored_heatmap*0.7 + resized_image*0.3
            cv2.circle(masked_image,
                       (int(preds[i][j][0]), int(preds[i][j][1])),
                       1, [0, 0, 255], 1)

            width_begin = heatmap_width * (j+1)
            width_end = heatmap_width * (j+2)
            grid_image[height_begin:height_end, width_begin:width_end, :] = \
                masked_image

        grid_image[height_begin:height_end, 0:heatmap_width, :] = resized_image

    cv2.imwrite(file_name, grid_image)

def get_max_preds(batch_heatmaps):
    """
    get predictions from score maps
    heatmaps: numpy.ndarray([batch_size, num_joints, height, width])
    """
    assert isinstance(batch_heatmaps, np.ndarray), \
        'batch_heatmaps should be numpy.ndarray'
    assert batch_heatmaps.ndim == 4, 'batch_images should be 4-ndim'

    batch_size = batch_heatmaps.shape[0]
    num_joints = batch_heatmaps.shape[1]
    width = batch_heatmaps.shape[3]
    heatmaps_reshaped = batch_heatmaps.reshape((batch_size, num_joints, -1))
    idx = np.argmax(heatmaps_reshaped, 2)
    maxvals = np.amax(heatmaps_reshaped, 2)

    maxvals = maxvals.reshape((batch_size, num_joints, 1))
    idx = idx.reshape((batch_size, num_joints, 1))

    preds = np.tile(idx, (1, 1, 2)).astype(np.float32)

    preds[:, :, 0] = (preds[:, :, 0]) % width
    preds[:, :, 1] = np.floor((preds[:, :, 1]) / width)

    pred_mask = np.tile(np.greater(maxvals, 0.0), (1, 1, 2))
    pred_mask = pred_mask.astype(np.float32)

    preds *= pred_mask
    return preds, maxvals

if __name__ == '__main__':
    args = parser.parse_args()
    train(args)

