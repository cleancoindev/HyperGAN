# https://arxiv.org/pdf/1911.00888v1.pdf

import importlib
import json
import numpy as np
import os
import sys
import time
import uuid
import copy

from hypergan.discriminators import *
from hypergan.distributions import *
from hypergan.generators import *
from hypergan.inputs import *
from hypergan.samplers import *
from hypergan.trainers import *

import hyperchamber as hc
from hyperchamber import Config
from hypergan.ops import TensorflowOps
import hypergan as hg

from hypergan.gan_component import ValidationException, GANComponent
from .base_gan import BaseGAN

from hypergan.distributions.uniform_distribution import UniformDistribution
from hypergan.trainers.experimental.consensus_trainer import ConsensusTrainer

class MultiMarginalGAN(BaseGAN):
    """ 
    """
    def __init__(self, *args, **kwargs):
        BaseGAN.__init__(self, *args, **kwargs)

    def required(self):
        """
        `input_encoder` is a discriminator.  It encodes X into Z
        `discriminator` is a standard discriminator.  It measures X, reconstruction of X, and G.
        `generator` produces two samples, input_encoder output and a known random distribution.
        """
        return "generator discriminator ".split()

    def create(self):
        config = self.config
        ops = self.ops

        with tf.device(self.device):
            def random_like(x):
                return UniformDistribution(self, config.latent, output_shape=self.ops.shape(x)).sample
            self.latent = self.create_component(config.latent, name='latent')

            xs = self.inputs.datasets


            # xs[0] = source dataset from initial domain
            # xs[1] = dataset from target domain 1
            # xs[2] = dataset from target domain 2
            # ...

            print("Number of target domains: ", len(xs)-1)

            def gen(source, count):
                generators = []
                for i in range(count):
                    generators.append(self.create_component(config.generator, input=source, name='generator_'+str(i), reuse=False))
                return generators

            def enc(source, reuse=False):
                return self.create_component(config.encoder, input=source, name='encoder', reuse=reuse)


            def interp(source, targets):
                interps = []
                for target in targets:
                    p_uniform = tf.random_uniform([1], 0, 1)
                    interps.append(source * p_uniform + target.sample * (1 - p_uniform))
                return interps

            encoder = enc(self.inputs.x)

            if config.shared_encoder:
                x_hats = gen(encoder.sample, len(xs)-1)
            else:
                x_hats = gen(self.inputs.x, len(xs)-1)
            self.x_hats = x_hats
            x_hat = x_hats[0].sample

            x_interps = interp(self.inputs.x, x_hats)

            interdomain_lambda = config.interdomain_lambda or 1

            self.ga = x_hat
            self.gb = x_hat

            self.uniform_sample = x_hat

            t0 = self.inputs.x

            stack = [t0] + [x_h.sample for x_h in x_hats]
            stacked = ops.concat(stack, axis=0)
            features = None
            self.features = features

            d = self.create_component(config.discriminator, name='discriminator',
                    input=stacked, features=[features])

            self.discriminator = d
            l = self.create_loss(config.loss, d, None, None, len(stack))
            self.loss = l
            self.losses = [self.loss]
            self.standard_loss = l
            self.z_loss = l
            loss1 = l
            d_loss1 = l.d_loss * (self.config.d_lambda or 1)
            g_loss1 = l.g_loss * (self.config.d_lambda or 1)
            d_loss2 = tf.identity(d_loss1)
            g_loss2 = tf.identity(g_loss1)
            c_loss = None


            d_vars1 = d.variables()
            c_vars = d.variables()
            if config.shared_encoder:
                g_vars1 = encoder.variables()
            else:
                g_vars1 = []

            for i in range(len(x_hats)):
                x_h = x_hats[i].sample
                x_source = xs[i+1]
                x_interp = x_interps[i]
                d2 = self.create_component(config.discriminator, name='discriminator',
                        input=tf.concat([x_source, x_h], axis=0), features=[features], reuse=True)
                l_g = self.create_loss(config.mi_loss or config.loss, d2, None, None, 2)
                mi_g_loss = l_g.g_loss
                mi_d_loss = l_g.d_loss
                grad_penalty_lambda = config.gradient_penalty_lambda or 10

                d3 = self.create_component(config.discriminator, name='discriminator',
                        input=x_interp, features=[features], reuse=True)

                lf = config.lf or 1
                self.add_metric("mi_g", mi_g_loss * interdomain_lambda)
                self.add_metric("mi_d", mi_d_loss * interdomain_lambda)

                d_loss1 += mi_d_loss * interdomain_lambda
                g_loss1 += mi_g_loss * interdomain_lambda
                g_vars1 += x_hats[i].variables()

                # for custom trainer
                if config.gradient_penalty:
                    gds = [gd for gd in tf.gradients(d3.sample, d_vars1) if gd is not None]
                    grad_penalty = [tf.reduce_sum(tf.square(gd)) for gd in gds]
                    grad_penalty = [tf.sqrt(gd) for gd in gds]
                    grad_penalty = [tf.reduce_max(tf.nn.relu(_grad - lf)) for _grad in grad_penalty]
                    grad_penalty = tf.square(tf.add_n(grad_penalty)/len(grad_penalty))
                    d_loss1 += grad_penalty * grad_penalty_lambda
                    d_loss2 += grad_penalty * grad_penalty_lambda

                if c_loss == None:
                    c_loss = mi_d_loss * interdomain_lambda
                else:
                    c_loss += mi_d_loss * interdomain_lambda
                g_loss2 += mi_g_loss * interdomain_lambda

                if config.l1_loss:
                    l1 = config.l1_loss * tf.reduce_mean(tf.abs(x_hats[i].reuse(enc(x_source, reuse=True).sample) - x_source))
                    g_loss1 += l1
                    g_loss2 += l1
                    self.add_metric('l1', l1)

                if config.classifier:
                    labels = tf.constant([1 if i==j else 0 for j in range(len(x_hats))])
                    logits = d2.named_layers['classifier']
                    print("---", logits, labels, self.gan.batch_size())
                    labels = tf.tile(labels, [self.gan.batch_size()*2])
                    labels = tf.reshape(labels, self.ops.shape(logits))
                    softmax = tf.nn.softmax_cross_entropy_with_logits( labels=labels, logits=logits )
                    g_loss1 += softmax
                    g_loss2 += softmax
                    c_loss += softmax
                    d_loss1 += softmax
                    self.add_metric('c', softmax)

            self.generator = x_hats[0]

            d_loss = d_loss1
            g_loss = g_loss1


            metrics = {
                    'g_loss': l.g_loss,
                    'd_loss': l.d_loss,
                    'c_loss': c_loss
                }

            self._g_vars = g_vars1
            self._d_vars = d_vars1
            self._c_vars = c_vars

            self.loss = hc.Config({
                'd_fake':l.d_fake,
                'd_real':l.d_real,
                'sample': [d_loss1, g_loss1],
                'sample_cdg': [c_loss, d_loss2, g_loss2],
                'metrics': metrics
                })
            trainer = self.create_component(config.trainer)

            self.initialize_variables()

        self.trainer = trainer

    def d_vars(self):
        return self._d_vars

    def g_vars(self):
        return self._g_vars

    def c_vars(self):
        return self._c_vars

    def trainable_c_vars(self):
        return list(set(self.c_vars()).intersection(tf.trainable_variables()))

    def fitness_inputs(self):
        return [
            self.uniform_distribution.sample, self.inputs.xa
        ]

    def create_loss(self, loss_config, discriminator, x, generator, split):
        loss = self.create_component(loss_config, discriminator = discriminator, x=x, generator=generator, split=split)
        return loss

    def create_cycloss(self, x_input, x_hat):
        config = self.config
        ops = self.ops
        distance = config.distance or ops.lookup('l1_distance')
        cycloss_lambda = config.cycloss_lambda
        if cycloss_lambda is None:
            cycloss_lambda = 10

        cycloss = tf.reduce_mean(distance(x_input, x_hat))

        cycloss *= cycloss_lambda
        return cycloss

    def input_nodes(self):
        "used in hypergan build"
        if hasattr(self.generator, 'mask_generator'):
            extras = [self.mask_generator.sample]
        else:
            extras = []
        return extras + [
                self.x_input
        ]


    def output_nodes(self):
        "used in hypergan build"

        if hasattr(self.generator, 'mask_generator'):
            extras = [
                self.mask_generator.sample, 
                self.generator.g1x,
                self.generator.g2x
            ]
        else:
            extras = []
        return extras + [
                self.encoder.sample,
                self.generator.sample, 
                self.uniform_sample,
                self.generator_int
        ]