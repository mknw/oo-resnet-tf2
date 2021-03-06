"""
Mask R-CNN with TF 2.0 - alpha
TODOs:
1. crop some instead of padding all to max (now proportion of zeros is 0.6 on average for the first batch)
2. check whether softmax_crossentropy_with_logits can be used here (imagenet categories are mutually exclusive)
3. Configure distribute training
"""


import os
import random
import datetime
import re
import math
import numpy as np
import tensorflow as tf
from tensorflow.keras.layers import Layer, GlobalAveragePooling2D, Conv2D, BatchNormalization, Activation, MaxPooling2D, Dense
from tensorflow.keras import Model
from utils import norm_zero_centred, test_model, LearningRateReducer

'''
TODO:
- LearningRateReducer:
	1. tune plateau_range
	2. At the end of training, learning rate always change. We could make plateau_range adaptive too (e.g. multiplied by LRR.factor)
- layer regularization
'''

from distutils.version import LooseVersion
assert LooseVersion(tf.__version__) >= LooseVersion("2.0.0")


class ResNet(Model):
	
	def __init__(self, input_shape, output_dim, config):
		
		super(ResNet, self).__init__()
		# assert config.BACKBONE in ['51', '101']
		self.train_bn = config.TRAIN_BN# we turn off training for small batch size. 
		print('found config.TRAIN_BN: ' + str(config.TRAIN_BN))

		'''
		TODO: add regularization loss (weight decay = 0.0001
		This is done via the conv2d (and possibly other layers) arg: kernel_regularizer.
		Possible solution can be 'tf.contrib.layers.l2_regularizer'.
		'''

		""" conv1 """
		# Here the output shape is NOT specified, 
		# whereas in ResNet output size should be 112x112. 
		self.conv1 = Conv2D(64, input_shape=input_shape, kernel_size=(7,7),strides=(2,2), padding='same', name='conv1')
		self.bn1 = BatchNormalization(trainable=self.train_bn)

		""" conv2_x """
		st= '2' # stage
		blks = [l for l in 'abcdefghijklmnopqrstuvwxyz'] # blocks appendices
		# MaxPool in conv2_x for consistency with He et al. 2015
		self.pool1 = MaxPooling2D(pool_size=(3,3), strides=(2,2), padding='same')

		# channel out is the first arg. 
		# if channel_in is not given, channel_out = channel_in
		self.conv2 = self._building_block(st+blks[0], 256, channel_in=64) 
		self.block2 = [self._building_block(st+blks[i+1],256) for i in range(2)]

		""" conv3_x """
		st = '3' # stage 3
		self.conv3 = self._building_block(st+blks[0], 
								 512, channel_in=256, downsample=True)
		self.block3 = [self._building_block(st+blks[i+1], 512) for i in range(3)]

		""" conv4_x """ 
		st = '4' # stage 4
		n_blocks = {'resnet51': 6, 'resnet101': 23}[config.BACKBONE]

		self.conv4 = self._building_block(st+blks[0],
									1024, channel_in=512, downsample=True)
		self.block4 = [self._building_block(st+blks[i+1],1024) for i in range(n_blocks-1)]
		
		""" conv5_x """
		st = '5' # stage 5
		self.conv5 = self._building_block(st+blks[0],
								2048, channel_in=1024, downsample=True)
		self.block5 = [self._building_block(st+blks[i+1], 2048) for i in range(2)]

		""" dense """
		self.avg_pool = GlobalAveragePooling2D()
		self.fc = Dense(1000, activation='relu')
		self.out = Dense(output_dim, activation='softmax')

	def call(self, x):
		# might have to rename convs in comments with 0-index based terminology
		h = self.conv1(x) # conv1
		h = self.bn1(h)
		h = tf.nn.relu(h)
		h = self.pool1(h) # start conv2
		h = self.conv2(h) # conv2_1
		for block in self.block2:
			h = block(h)
		h = self.conv3(h) # start conv3
		for block in self.block3:
			h = block(h)
		h = self.conv4(h) # start conv4
		for block in self.block4:
			h = block(h)
		h = self.conv5(h) # start conv5
		for block in self.block5:
			h = block(h)
		h = self.avg_pool(h) # start dense
		h = self.fc(h) # fully connected
		y = self.out(h) # softmax
		return y # TODO: return h2, h3, h4 and h5 after each conv. stage. 

	def stage_summary(self, stage_index):

		if isinstance(stage_index, int):
			stage_index = [stage_index]

		stages = {2: [self.conv2, self.block2],
				      3: [self.conv3, self.block3],
							4: [self.conv4, self.block4],
							5: [self.conv5, self.block5]}

		import ipdb; ipdb.set_trace()
		for i in stage_index:
			stages[i][0].summary()
			[b.summary() for b in stages[i][1]]


		super(ResNet, self).summary()


	def _building_block(self, st_bl_name, channel_out, channel_in=None, 
			downsample=False):
		if channel_in is None:
			channel_in = channel_out
		return Block(st_bl_name, channel_in, channel_out, downsample, self.train_bn)


class Block(Model): # WAS LAYER """ResNet101 building block"""
	def __init__(self, st_bl_name, channel_in, channel_out, downsample=False,
							 train_bn=True):
		
		super(Block, self).__init__()
		
		'''
		'filters': as in the dimensionality of the output space 
		(i.e. the number of output filter in the convolution).
		In Resnet paper, 101 
		'''

		if not downsample:
			strides = (1, 1)
			pass
		else:
			strides = (2, 2)

		conv_basename = 'res' + st_bl_name + '_branch'
		bn_basename = 'bn' + st_bl_name + '_branch'

		filters = channel_out // 4
		self.conv1 = Conv2D(filters, kernel_size=(1,1), strides=strides,
				padding='same', name=conv_basename + '2a')
		self.bn1 = BatchNormalization(name=bn_basename + '2a', trainable=train_bn)

		self.conv2 = Conv2D(filters, kernel_size=(3, 3), padding='same',
				name = conv_basename + '2b')
		self.bn2 = BatchNormalization(name = bn_basename + '2b', trainable=train_bn)

		self.conv3 = Conv2D(channel_out, kernel_size=(1,1), padding='same',
				name = conv_basename + '2c')
		self.bn3 = BatchNormalization(name = bn_basename + '2c', trainable=train_bn)
		# here conv_basename is used only if self._shortcut is a convolutional identity block
		# (else the value is unused)
		self.shortcut = self._shortcut(channel_in, channel_out, strides, conv_basename+'1') 

	def call(self, x):
		h = self.conv1(x)
		h = self.bn1(h)
		h = tf.nn.relu(h)

		h = self.conv2(h)
		h = self.bn2(h)
		h = tf.nn.relu(h)
		
		h = self.conv3(h)
		h = self.bn3(h)
		shortcut = self.shortcut(x)

		h += shortcut
		return tf.nn.relu(h)
	
	def _shortcut(self, channel_in, channel_out, strides, name):
		"""
		Identity mappings if in- and out-put are same size.
		Else, project with 1*1 convolutions. 
		This allows to make every first layer of a ConvN_X block
		a (convolutional) projection, whereas the following
		are identity mappings. 
		"""
		# channel_in and channel_out are always referred to the 
		# bottleneck blocks
		if channel_in != channel_out:
			return self._projection(channel_out, name, strides)
		else:
			return lambda x: x # Lambda layer could allow us to give it a name.

	def _projection(self, channel_out, name, strides):
		return Conv2D(channel_out, kernel_size=(1, 1), padding='same',
						strides=strides, name=name) # strides=strides)

	
	

if __name__ =='__main__':

	''' GPU(s) '''
	from argparse import ArgumentParser

	parser = ArgumentParser()
	parser.add_argument("-gpu", dest="gpu", default=4, type=int)
	parser.add_argument("-viz-dir", dest="viz_dir", default="./imgs/")
	args = parser.parse_args()


	gpus = tf.config.experimental.list_physical_devices('GPU')
	GPU_N = args.gpu
	if gpus:
		try:
			tf.config.experimental.set_visible_devices(gpus[GPU_N], 'GPU')
			logical_gpus = tf.config.experimental.list_logical_devices('GPU')
			print(len(gpus), "Physical GPUs,", len(logical_gpus), "Logical GPUs")
		except RuntimeError as e:
			print(e)
			import ipdb; ipdb.set_trace()

	
	np.random.seed(420)
	tf.random.set_seed(420)

	'''
	loss and gradient function.
	'''

	# @tf.function
	def loss(model, x, y):
		y_ = model(x)
		# import ipdb; ipdb.set_trace()
		return loss_object(y_true=y, y_pred=y_)
	
	@tf.function
	def smooth_l1_loss(model, x, y):
		"""Implements Smooth-L1 loss.
		y_true and y_pred are typically: [N, 4], but could be any shape.
		"""
		import ipdb; ipdb.set_trace()
		y_ = model(x)
		deltas = tf.abs(y - y_)
		less_than_one = tf.keras.cast(tf.less(deltas, 1.0), "float32")
		lossl1 = (less_than_one * 0.5 * deltas**2) + (1 - deltas) * (deltas - 0.5)
		return lossl1

	@tf.function
	def grad(model, inputs, targets):
		with tf.GradientTape() as tape:
			loss_value = loss(model, inputs, targets)
		return loss_value, tape.gradient(loss_value, model.trainable_variables)


	''' dataset and dataset iterator'''
	# cifar100 = tf.keras.datasets.cifar100
	# (x_train, y_train), (x_test, y_test) = cifar100.load_data(label_mode='fine')

	import tensorflow_datasets as tfds

	tfds.list_builders()
	imagenet2012_builder = tfds.builder("imagenet2012")
	specs = imagenet2012_builder.info
	train_instances = specs.splits['train'].num_examples
	val_instances = specs.splits['validation'].num_examples

	train_set, val_set = imagenet2012_builder.as_dataset(split=["train", "validation"])

	train_set = train_set.shuffle(1024).map(norm_zero_centred)
	train_set = train_set.batch(32)

	val_set = val_set.shuffle(1024).map(norm_zero_centred)
	val_set = val_set.batch(32)
	
	''' model '''
	from viz import *
	from utils import test_model, norm_zero_centred, LearningRateReducer
	from config import Config
	from progress import Regbar


	C = Config()
	C.BATCH_SIZE = 32
	C.INPUT_SIZE = (256, 256)

	C.BACKBONE = 'resnet51'
	# best course of action for input shape declaration?
	model = ResNet((256, 256, 3), output_dim=1000, config=C)
	model.build(input_shape=(C.BATCH_SIZE, 256, 256, 3)) # place correct shape from imagenet

	''' initialize '''
	# Reduce LR with *0.1 when plateau is detected
	# adapt_lr = LearningRateReducer(init_lr=0.1, factor=0.1,
	# 					patience=10, refractory_interval=20) # wait 20 epochs from last update
	loss_object = tf.losses.SparseCategoricalCrossentropy()
	# optimizer = tf.keras.optimizers.SGD(adapt_lr.monitor(), momentum = 0.9)
	optimizer = tf.keras.optimizers.Adam(0.001) # haven't tried this yet.. 
	

	train_loss_results = []
	train_accuracy_results = []
	test_loss_results, test_acc_results = [], []

	num_epochs = 300
	prev_epoch_loss_avg = 0.
	n_train_btchs =  int(train_instances) // C.BATCH_SIZE

	
	''' train '''

	import ipdb; ipdb.set_trace()


	for epoch in range(1,num_epochs):
	
		progbar = Regbar(n_train_btchs,stateful_vars=['batch CCE', 'epoch accuracy'])

		epoch_loss_avg = tf.keras.metrics.Mean()
		epoch_accuracy = tf.keras.metrics.SparseCategoricalAccuracy()
		k = 0
		
		# optimizer = tf.keras.optimizers.SGD(adapt_lr.monitor(train_loss_results), momentum = 0.9)

		for batch in train_set:
			# img_btch, lab_btch, fn_btch = batch
			img_btch = batch['image']
			lab_btch = batch['label']
			loss_value, grads = grad(model, img_btch, lab_btch)
			optimizer.apply_gradients(zip(grads, model.trainable_variables))
			# track progress.
			epoch_loss_avg(loss_value)
			epoch_accuracy(lab_btch, model(img_btch))

			# print("Epoch {:03d}: Batch: {:03d} Loss: {}, Accuracy: {}".format(epoch, k,  epoch_loss_avg.result(), epoch_accuracy.result()))
			progbar.update(k, values=[('batch CCE', loss_value),
					                          ('epoch accuracy', epoch_accuracy.result())])
			k+=1
			# prev_epoch_loss_avg = epoch_loss_avg.result()


		print("Trainset >> Epoch {:03d}: Loss: {}, Accuracy: {}".format(epoch, epoch_accuracy.result()))
		# end epoch

		#if int(epoch_accuracy.result() > 70):
		test_loss, test_accuracy = test_model(model, val_set)

		test_loss_results.append(test_loss)
		test_acc_results.append(test_accuracy)
		train_loss_results.append(epoch_loss_avg.result())
		train_accuracy_results.append(epoch_accuracy.result())

		if epoch % 100 == 0:
			fname = os.path.join(args.viz_dir,  '/Test_Acc_Loss_IN2012_' + str(epoch) + '.png')

			loss_l = [train_loss_results, test_loss_results]
			acc_l = [train_accuracy_results, test_acc_results]
			save_plot(loss_l, acc_l, fname) # plotting both train and validation. 
	

	import ipdb; ipdb.set_trace()
