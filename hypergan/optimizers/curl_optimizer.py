#From https://gist.github.com/EndingCredits/b5f35e84df10d46cfa716178d9c862a3
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import state_ops
from tensorflow.python.framework import ops
from tensorflow.python.training import optimizer
import tensorflow as tf
import hyperchamber as hc
import inspect

class CurlOptimizer(optimizer.Optimizer):
  def __init__(self, learning_rate=0.001, p=0.1, gan=None, config=None, use_locking=False, name="CurlOptimizer", optimizer=None, rho=1, beta=1, gamma=1):
    super().__init__(use_locking, name)
    self._beta = beta
    self._rho = rho
    self._gamma = gamma
    self.gan = gan
    self.config = config
    self._lr_t = learning_rate
    self.g_rho = gan.configurable_param(self.config.g_rho)
    self.d_rho = gan.configurable_param(self.config.d_rho)
    if tf.contrib.framework.is_tensor(self.g_rho):
        self.gan.add_metric("g_rho", self.g_rho)
    if tf.contrib.framework.is_tensor(self.d_rho):
        self.gan.add_metric("d_rho", self.d_rho)
    def create_optimizer(klass, options):
        options['gan']=self.gan
        options['config']=options
        defn = {k: v for k, v in options.items() if k in inspect.getargspec(klass).args}
        learn_rate = options.learn_rate or options.learning_rate
        if 'learning_rate' in options:
            del defn['learning_rate']
        return klass(learn_rate, **defn)

    optimizer = hc.lookup_functions(optimizer)
    self.optimizer = create_optimizer(optimizer['class'], optimizer)
 
  def _prepare(self):
    super()._prepare()
    self.optimizer._prepare()

  def _create_slots(self, var_list):
    super()._create_slots(var_list)
    self.optimizer._create_slots(var_list)

  def _apply_dense(self, grad, var):
    return self.optimizer._apply_dense(grad, var)

  def apply_gradients(self, grads_and_vars, global_step=None, name=None):
    var_list = [ v for _,v in grads_and_vars]
    d_vars = []
    g_vars = []
    for grad,var in grads_and_vars:
        if var in self.gan.d_vars():
            d_vars += [var]
        elif var in self.gan.g_vars():
            g_vars += [var]
        else:
            raise("Couldn't find var in g_vars or d_vars")

    with ops.init_scope():
        gswap = [self._zeros_slot(v, "gswap", self._name) for _,v in grads_and_vars]
        v1 = [self._zeros_slot(v, "v1", self._name) for _,v in grads_and_vars]
        slots_list = []
        if self.config.include_slots:
            for name in self.optimizer.get_slot_names():
                for var in self.optimizer.variables():
                    slots_list.append(self._zeros_slot(var, "curl", "curl"))
    self._prepare()

    gswap = [self.get_slot(v, "gswap") for _,v in grads_and_vars]
    v1 = [self.get_slot(v, "v1") for _,v in grads_and_vars]
    slots_list = []
    slots_vars = []
    if self.config.include_slots:
        for name in self.optimizer.get_slot_names():
            for var in self.optimizer.variables():
                slots_vars += [var]
                slots_list.append(self._zeros_slot(var, "curl", "curl"))


    restored_vars = var_list + slots_vars
    tmp_vars = v1 + slots_list
    tmp_grads = gswap
    all_grads = [ g for g, _ in grads_and_vars ]
    # store variables for resetting

    consensus_grads = all_grads#[:len(d_vars)]
    if self.config.g_beta is None:
        consensus_grads = all_grads[:len(d_vars)]

    if self.config.beta_type == 'sga':
        Jgrads = tf.gradients(consensus_grads, d_vars, grad_ys=consensus_grads, stop_gradients=d_vars) + [tf.zeros_like(g) for g in g_vars]
    elif self.config.beta_type == 'magnitude':
        consensus_reg = [tf.square(g) for g in consensus_grads if g is not None]
        Jgrads = tf.gradients(consensus_reg, d_vars) + [tf.zeros_like(g) for g in g_vars]
    else:
        consensus_reg = 0.5 * sum(
                tf.reduce_sum(tf.square(g)) for g in consensus_grads if g is not None
        )
        Jgrads = tf.gradients(consensus_reg, d_vars, stop_gradients=d_vars) + [tf.zeros_like(g) for g in g_vars]

    op1 = tf.group(*[tf.assign(w, v) for w,v in zip(tmp_vars, restored_vars)]) # store variables
    op2 = tf.group(*[tf.assign(w, v) for w,v in zip(gswap, all_grads)]) # store gradients

    with tf.get_default_graph().control_dependencies([op1, op2]):
        # store g2
        op3 = tf.group(*[tf.assign_sub(v, self._lr_t*grad) for grad,v in grads_and_vars])
        with tf.get_default_graph().control_dependencies([op3]):

            def curlcombine(g1,g2,_v1,_v2,curl,rho):
                #stepsize = (_v2-_v1)/(g1+1e-8)
                stepsize = self._lr_t
                if curl == "mirror":
                    return self._gamma*(g1 + 2*g2)
                else:
                    return self._gamma*g1-rho*(g2-g1)/stepsize
            g2s = tf.gradients(self.gan.trainer.d_loss, d_vars) + tf.gradients(self.gan.trainer.g_loss, g_vars)
            if self.config.form == 'central':
                def central_step():
                    # restore v1, slots
                    op5 = tf.group(*[ tf.assign(w,v) for w,v in zip(restored_vars, tmp_vars)])
                    with tf.get_default_graph().control_dependencies([op5]):
                        back =  tf.group(*[tf.assign_sub(v, -self._lr_t*grad) for grad,v in grads_and_vars])
                        with tf.get_default_graph().control_dependencies([back]):
                            return tf.gradients(self.gan.trainer.d_loss, d_vars) + tf.gradients(self.gan.trainer.g_loss, g_vars)
                def curlcombinecentral(g1,g2,_v1,_v2,curl,rho):
                    #stepsize = (_v2-_v1)/(g1+1e-8)
                    stepsize = self._lr_t
                    if curl == "mirror":
                        return self._gamma*(g1 + 2*g2)
                    else:
                        return self._gamma*g1-rho*(g2-g1)/(2*stepsize)

                g1s  = central_step()
                g3s = [curlcombinecentral(g1,g2,v1,v2,self.config.d_curl,self.d_rho) if v2 in d_vars else curlcombinecentral(g1,g2,v1,v2,self.config.g_curl,self.g_rho) for g1,g2,v1,v2 in zip(g1s,g2s,v1,var_list)]
            else:
                #forward
                g3s = [curlcombine(g1,g2,v1,v2,self.config.d_curl,self.d_rho) if v2 in d_vars else curlcombine(g1,g2,v1,v2,self.config.g_curl,self.g_rho) for g1,g2,v1,v2 in zip(gswap,g2s,v1,var_list)]
            # restore v1, slots
            op5 = tf.group(*[ tf.assign(w,v) for w,v in zip(restored_vars, tmp_vars)])
            with tf.get_default_graph().control_dependencies([op5]):
                flin = []
                for grad, jg in zip(g3s, Jgrads):
                    if jg is None or self._beta <= 0:
                        flin += [grad]
                    else:
                        flin += [grad + jg * self._beta]

                if self.config.orthonormal:
                    shapes = [self.gan.ops.shape(l) for l in flin]
                    u = [tf.reshape(l, [-1]) for l in flin[:len(d_vars)]]
                    v = [tf.reshape(l, [-1]) for l in Jgrads[:len(d_vars)]]
                    
                    def proj(u, v,shape):
                        dot = tf.tensordot(v, u, 1) / (tf.square(u)+1e-8)
                        dot = tf.maximum(-1.0, dot)
                        dot = tf.minimum(1.0, dot)
                        dot = dot * u
                        dot = tf.reshape(dot, shape)
                        return dot
                    proj_u1_v2 = [proj(_u, _v, _s) for _u, _v, _s in zip(u, v, shapes)]
                    flin = [_flin + self.gan.configurable_param(self.config.ortholambda) * proj for _flin, proj in zip(flin, proj_u1_v2)] + flin[len(d_vars):]

                step3 = list(zip(flin, var_list))
                op6 = self.optimizer.apply_gradients(step3.copy(), global_step=global_step, name=name)

                with tf.get_default_graph().control_dependencies([op6]):
                    return tf.no_op()

  def _apply_sparse(self, grad, var):
    raise NotImplementedError("Sparse gradient updates are not supported.")
  def variables(self):
      return self.optimizer.variables()