import collections
import logging
from functools import partial
import pathlib
import os
import pickle
import itertools

import numpy as np
import tensorflow as tf

from tensionflow import util

logger = logging.getLogger(__name__)


class Dataset(object):
    def __init__(self,
                 filepath=None,
                 splits=('training', 'test', 'validation'),
                 preprocessor=None,
                 indexify_labels=True):
        self.meta = {}
        self.splits = {}
        self.meta['data_struct'] = tuple()
        self.errors = []
        if filepath:
            logger.info(f'Loading {splits} datasets from {filepath}')
            with open(self.metafile(filepath), 'rb') as handle:
                self.meta = pickle.load(handle)
            for split in splits:
                self.splits[split] = self.load(filepath, split)
        else:
            for split in splits:
                X, Y = self.load_features_and_labels(split)
                if indexify_labels:
                    Y = self.indexify(Y, split)
                self.splits[split] = self.generate_dataset(X, Y, preprocessor)

    def load_features_and_labels(self, split):
        """Load or generate data in the form (features, labels)"""
        raise NotImplementedError("Override this method to return (X, Y)")

    def indexify(self, labels, split='training'):
        """Convert string labels to indices in the range (0, num_labels)"""
        if split == 'training':
            Y, self.meta['label_dict'] = util.indexify(labels)
        else:
            Y, _ = util.indexify(labels, self.meta['label_dict'])
        return Y

    def generate_dataset(self, X, Y, preprocessor=None):
        """Generate tensorflow Dataset API from features and labels"""
        if preprocessor is None:
            preprocessor = util.identity_func
        logger.info('Determining processed dtypes...')
        x1, y1 = (next(iter(i or []), None) for i in (X, Y))
        x, y = preprocessor(x1, y1)
        x, y = np.asarray(x), np.asarray(y)
        x_dtype = tf.as_dtype(x.dtype)
        y_dtype = tf.as_dtype(y.dtype)
        logger.info(f'Preprocess dtypes: {(x_dtype, y_dtype)}')

        def gen(X, Y):
            for x, y in zip(X, Y):
                try:
                    data = preprocessor(x, y)
                    self.meta['data_struct'] = tuple(
                        parse_data_structure(new, old)
                        for new, old in itertools.zip_longest(
                            data, self.meta['data_struct']))
                    yield data
                except Exception as e:
                    logger.warning(f'Error preprocessing value: {x}')
                    logger.warning(e)
                    self.errors.append((x, y))

        data_gen = partial(gen, X, Y)
        dataset = tf.data.Dataset.from_generator(data_gen, (x_dtype, y_dtype))
        # TODO: shuffle before preprocessor
        # dataset = dataset.shuffle(buffer_size=len(X))
        return dataset

    def dump(self, output, splits=None, overwrite=False):
        """Save dataset(s) as tfrecords"""
        pathlib.Path(output).mkdir(parents=True, exist_ok=overwrite)
        if splits is None:
            splits = [key for key in self.splits]
        for split in splits:
            filename = self.datasetfile(output, split)
            logger.info(f"Saving split '{split}' to {filename}")
            dataset = self.splits[split]
            with tf.Session() as sess, tf.python_io.TFRecordWriter(
                    filename) as writer:
                next_element = dataset.make_one_shot_iterator().get_next()
                while True:
                    try:
                        x, y = sess.run(next_element)
                        features = util.map_if_collection(
                            util._dtype_feature, x)
                        if not isinstance(features, collections.Iterable):
                            features = [features]
                        feature_list = {
                            'feature_list': {
                                'x': tf.train.FeatureList(feature=features),
                            }
                        }
                        context = {
                            'feature': {
                                'y': util._dtype_feature(y),
                            },
                        }
                        example = tf.train.SequenceExample(
                            feature_lists=feature_list, context=context)
                        writer.write(example.SerializeToString())
                    except tf.errors.OutOfRangeError:
                        break
        filename = self.metafile(output)
        logger.info(f"Saving metadata to {self.metafile(output)}")
        with open(filename, 'wb') as handle:
            pickle.dump(self.meta, handle, protocol=pickle.HIGHEST_PROTOCOL)

    def load(self, filepath, split='training', dtypes=None):
        """Load a dataset from a tfrecord"""
        if dtypes is None:
            dtypes = {
                'labels': tf.int64,
                'features': tf.float32
            }
        x_struct, _ = self.meta['data_struct']

        def _parse_function(example):
            sequence_features = {
                'x':
                tf.FixedLenSequenceFeature(
                    x_struct['shape'][1:], dtype=dtypes['features'])
            }
            context_features = {
                'y': tf.VarLenFeature(dtype=dtypes['labels']),
            }
            context, sequence = tf.parse_single_sequence_example(
                example, context_features, sequence_features)
            features = sequence['x']
            labels = tf.sparse_tensor_to_dense(
                context['y'],
                default_value=util.default_of_type(dtypes['labels']))
            return features, labels

        dataset = tf.data.TFRecordDataset(self.datasetfile(filepath, split))
        dataset = dataset.map(_parse_function)
        return dataset

    def metafile(self, directory):
        return os.path.join(directory, 'metadata')

    def datasetfile(self, directory, split):
        return os.path.join(directory, f'{split}.tfrecord')


def parse_data_structure(array, prev_struct=None):
    array = np.asarray(array)
    dtype = tf.as_dtype(array.dtype)
    shape = min_shape = max_shape = array.shape
    if prev_struct:
        if prev_struct['dtype'] != dtype:
            logger.warning(
                f'dtype {dtype} does not match previous dtype: {prev_struct["dtype"]}'
            )

        def new_shape(shape, prev_shape):
            if len(shape) != len(prev_shape):
                logger.error(
                    f'data shape {shape} incompatible with previous data shape: {prev_shape}'
                )
            for new, old in zip(shape, prev_shape):
                if new == old:
                    yield new
                else:
                    yield -1

        shape = list(new_shape(shape, prev_struct['shape']))
        if sum(min_shape) > sum(prev_struct['min_shape']):
            min_shape = prev_struct['min_shape']
        if sum(max_shape) < sum(prev_struct['max_shape']):
            max_shape = prev_struct['max_shape']
    return {
        'dtype': dtype,
        'shape': shape,
        'min_shape': min_shape,
        'max_shape': max_shape
    }
