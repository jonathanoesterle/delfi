import abc
import numpy as np

from delfi.neuralnet.NeuralNet import NeuralNet
from delfi.neuralnet.Trainer import Trainer
from delfi.utils.meta import ABCMetaDoc

import theano
dtype = theano.config.floatX

class BaseInference(metaclass=ABCMetaDoc):
    def __init__(self, generator,
                 prior_norm=True, init_norm=False,
                 pilot_samples=100,
                 seed=None, verbose=True, **kwargs):
        """Abstract base class for inference algorithms

        Inference algorithms must at least implement abstract methods of this
        class.

        Parameters
        ----------
        generator : generator instance
            Generator instance
        prior_norm : bool
            If set to True, will z-transform params based on mean/std of prior
        pilot_samples : None or int
            If an integer is provided, a pilot run with the given number of
            samples is run. The mean and std of the summary statistics of the
            pilot samples will be subsequently used to z-transform summary
            statistics.
        seed : int or None
            If provided, random number generator will be seeded
        kwargs : additional keyword arguments
            Additional arguments used when creating the NeuralNet instance

        Attributes
        ----------
        observables : dict
            Dictionary containing theano variables that can be monitored while
            training the neural network.
        """
        self.seed = seed
        if seed is not None:
            self.rng = np.random.RandomState(seed=seed)
        else:
            self.rng = np.random.RandomState()
        self.verbose = verbose

        # bind generator, reset proposal attribute
        self.generator = generator
        self.generator.proposal = None

        # generate a sample to get input and output dimensions
        if ('n_inputs' not in kwargs) or ('n_outputs' not in kwargs):
            params, stats = generator.gen(1, skip_feedback=True, verbose=False)
            kwargs.update({'n_inputs': stats.shape[1:],
                           'n_outputs': params.shape[1]})

        kwargs.update({'seed': self.gen_newseed()})

        self.kwargs = kwargs

        # optional: z-transform output for obs (also re-centres x onto obs!)
        self.init_norm = init_norm
        if 'n_components' in kwargs.keys() and kwargs['n_components'] > 1:
            self.init_fcv = 0.8
        else:
            self.init_fcv = 0.0

        self.reinit_network()  # init network, then update self.kwargs['seed']

        # parameters for z-transform of params
        if prior_norm:
            # z-transform for params based on prior
            self.params_mean = self.generator.prior.mean
            self.params_std = self.generator.prior.std
        else:
            # parameters are set such that z-transform has no effect
            self.params_mean = np.zeros((kwargs['n_outputs']))
            self.params_std = np.ones((kwargs['n_outputs'],))

        # parameters for z-transform for stats
        if pilot_samples is not None and pilot_samples != 0:
            # determine via pilot run
            if seed is not None:  #reseed generator for consistent inits
                self.generator.reseed(self.gen_newseed())
            self.pilot_run(pilot_samples)
        else:
            # parameters are set such that z-transform has no effect
            self.stats_mean = np.zeros((kwargs['n_inputs'][0],))
            self.stats_std = np.ones((kwargs['n_inputs'][0],))

        # observables contains vars that can be monitored during training
        self.compile_observables()
        self.round = 0

    @abc.abstractmethod
    def loss(self):
        pass

    @abc.abstractmethod
    def run(self):
        pass

    def run_repeated(self, n_repeats=10, n_NN_inits_per_repeat=1, callback=None,
                    **kwargs):
        """Repeatedly run the method and collect results. Optionally, carry out
        several runs with the same initial generator RNG state but different
        neural network initializations.

        parameters
        ----------
        n_repeats : int
            Number of times to run the algorithm
        n_NN_inits : int
            Number of times to
        callback: function
            callback function that will be called after each run. It should take
            4 inputs: callback(log, train_data, posterior, self)
        kwargs : additional keyword arguments
            Additional arguments that will be passed to the run() method
        """
        posteriors, outputs, repeat_index = [], [], []
        for r in range(n_repeats):

            if n_NN_inits_per_repeat > 1:
                generator_seed = self.gen_newseed()

            for i in range(n_NN_inits_per_repeat):

                self.reset()
                if n_NN_inits_per_repeat > 1:
                    self.generator.reseed(generator_seed)

                log, train_data, posterior = self.run(**kwargs)

                if callback is not None:
                    outputs.append(callback(log, train_data, posterior, self))
                else:
                    outputs.append(None)
                posteriors.append(posterior)
                repeat_index.append(r)

        return posteriors, outputs, repeat_index

    def reinit_network(self):
        """Reinitializes the network instance (re-setting the weights!)
        """
        self.network = NeuralNet(**self.kwargs)
        self.svi = self.network.svi
        """update self.kwargs['seed'] so that reinitializing the network gives a
        different result each time unless we reseed the inference method"""
        self.kwargs['seed'] = self.gen_newseed()
        self.norm_init()

    def centre_on_obs(self):
        """ Centres first-layer input onto observed summary statistics

        Ensures x' = x - xo, i.e. first-layer input x' = 0 for x = xo.
        """

        self.stats_mean = self.obs.copy()

    def remove_hidden_biases(self):
        """ Resets all bias weights in hidden layers to zero.

        """
        def idx_hiddens(x):
            return x.name[0]=='h'

        for b in filter(idx_hiddens, self.network.mps_bp):
            b.set_value(np.zeros_like(b.get_value()))

    def conditional_norm(self, fcv = 0.8, tmu=None, tSig=None, h=None):
        """ Normalizes current network output at observed summary statistics


        Parameters
        ----------
        fcv : float
            Fraction of total that comes from uncertainty over components, i.e.
            Var[th] = E[Var[th|z]] + Var[E[th|z]]
                    =  (1-fcv)     +     fcv       = 1
        tmu: array
            Target mean.
        tSig: array
            Target covariance.

        """

        # avoiding CDELFI.predict() attempt to analytically correct for proposal
        print('obs', self.obs.shape)
        print('mean', self.stats_mean.shape)
        print('std', self.stats_std.shape)
        obz = (self.obs - self.stats_mean) / self.stats_std
        posterior = self.network.get_mog(obz.reshape(self.obs.shape), deterministic=True)
        mog =  posterior.ztrans_inv(self.params_mean, self.params_std)

        assert np.all(np.diff(mog.a)==0.) # assumes uniform alpha

        n_dim = self.kwargs['n_outputs']
        triu_mask = np.triu(np.ones([n_dim, n_dim], dtype=dtype), 1)
        diag_mask = np.eye(n_dim, dtype=dtype)

        # compute MoG mean mu, Sig = E[Var[th|z]] and C = Var[E[th|z]]
        mu, Sig = np.zeros_like(mog.xs[0].m), np.zeros_like(mog.xs[0].S)
        for i in range(self.network.n_components):
            Sig += mog.a[i] * mog.xs[i].S
            mu  += mog.a[i] * mog.xs[i].m
        C = np.zeros_like(Sig)
        for i in range(self.network.n_components):
            dmu = mog.xs[i].m - mu if self.network.n_components > 1 else mog.xs[i].m
            C   += mog.a[i] * np.outer(dmu, dmu)

        # if not provided, target zero-mean unit variance (as for prior_norm=True)
        tmu = np.zeros_like(mog.xs[0].m) if tmu is None else tmu
        tSig = np.eye(mog.xs[0].m.size) if tSig is None else tSig

        # compute normalizers (we only z-score, don't whiten!)
        Z1inv = np.sqrt((1.-fcv) / np.diag(Sig) * np.diag(tSig)).reshape(-1)
        Z2inv = np.sqrt(  fcv    / np.diag( C ) * np.diag(tSig)).reshape(-1)

        # first we need the center of means
        def idx_MoG(x):
            return x.name[:5]=='means'
        mu_ = np.zeros_like(mog.xs[0].m)
        for w, b in zip(filter(idx_MoG, self.network.mps_wp),
                        filter(idx_MoG, self.network.mps_bp)):
            h = np.zeros(w.get_value().shape[0]) if h is None else h
            mu_ += h.dot(w.get_value()) + b.get_value()
        mu_ /= self.network.n_components

        # center and normalize means
        # mu =  Z2inv * (Wh + b - mu_) + tmu
        #    = Wh + (Z2inv * (b - mu_ + Wh) - Wh + tum)
        for w, b in zip(filter(idx_MoG, self.network.mps_wp),
                        filter(idx_MoG, self.network.mps_bp)):
            Wh = h.dot(w.get_value())
            b.set_value(Z2inv * (Wh + b.get_value() - mu_) - Wh + tmu)

        # normalize covariances
        def idx_MoG(x):
            return x.name[:10]=='precisions'
        # Sig^-0.5 = diag_mask * (exp(Wh+b)/exp(log(Z1)) + triu_mask * (Wh+b)*Z1
        #          = diag_mask *  exp(Wh+ (b-log(Z1))    + triu_mask * (Wh+((b+Wh)*Z1-Wh))
        for w, b in zip(filter(idx_MoG, self.network.mps_wp),
                        filter(idx_MoG, self.network.mps_bp)):
            Wh = h.dot(w.get_value()).reshape(n_dim,n_dim)
            b_ = b.get_value().copy().reshape(n_dim,n_dim)

            val = diag_mask * (b_ - np.diag(np.log(Z1inv))) + triu_mask * ((b_+Wh).dot(np.diag(1./Z1inv))- Wh )

            b.set_value(val.flatten())


    def norm_init(self):
        if self.init_norm:
            print('standardizing network initialization')
            if self.network.n_components > 1:
                self.standardize_init(fcv = self.init_fcv)
            else:
                self.standardize_init(fcv = 0.)

    def standardize_init(self, fcv = 0.8):
        """ Standardizes the network initialization on obs

        Ensures output distributions for xo have mean zero and unit variance.
        Alters hidden layers to propagates x=xo as zero to the last layer, and
        alters the MoG layers to produce the desired output distribution.
        """

        # ensure x' = x - xo
        self.centre_on_obs()

        # ensure x' = 0 stays zero up to MoG layer (setting biases to zero)
        self.remove_hidden_biases()

        # ensure MoG returns standardized output on x' = 0
        self.conditional_norm(fcv)

    def gen(self, n_samples, n_reps=1, prior_mixin=0, verbose=None, from_prior=False):
        """Generate from generator and z-transform

        Parameters
        ----------
        n_samples : int
            Number of samples to generate
        n_reps : int
            Number of repeats per parameter
        verbose : None or bool or str
            If None is passed, will default to self.verbose
        """
        verbose = self.verbose if verbose is None else verbose
        params, stats = self.generator.gen(n_samples, prior_mixin=prior_mixin, verbose=verbose, from_prior=from_prior)

        # z-transform params and stats
        params = (params - self.params_mean) / self.params_std
        stats = (stats - self.stats_mean) / self.stats_std

        return params, stats

    def reset(self, seed=None):
        """Resets inference method to a naive state, before it has seen any
        real or simulated data. The following happens, in order:
        1) The generator's proposal is set to None, and self.round is set to 0
        2) The inference method is reseeded if a seed is provided
        3) The network is reinitialized
        4) Any additional resetting of state specific to each inference method
        """
        self.generator.proposal = None
        self.round = 0
        if seed is not None:
            self.reseed(seed)
        self.reinit_network()

    def reseed(self, seed):
        """reseed inference method's RNG, then generator, then network"""
        self.rng.seed(seed=seed)
        self.seed = seed
        self.kwargs['seed'] = self.gen_newseed()   # for consistent NN init
        self.generator.reseed(self.gen_newseed())  # also reseeds prior + model
        self.network.reseed(self.gen_newseed())  # for reproducible MoG samples

    def gen_newseed(self):
        """Generates a new random seed"""
        if self.seed is None:
            return None
        else:
            return self.rng.randint(0, 2**31)

    def pilot_run(self, n_samples):
        """Pilot run in order to find parameters for z-scoring stats
        """

        verbose = '(pilot run) ' if self.verbose else False
        params, stats = self.generator.gen(n_samples, verbose=verbose)
        self.stats_mean = np.nanmean(stats, axis=0)
        self.stats_std = np.nanstd(stats, axis=0)

    def predict(self, x, deterministic=True):
        """Predict posterior given x

        Parameters
        ----------
        x : array
            Stats for which to compute the posterior
        deterministic : bool
            if True, mean weights are used for Bayesian network
        """
        x_zt = (x - self.stats_mean) / self.stats_std
        posterior = self.network.get_mog(x_zt, deterministic=deterministic)
        return posterior.ztrans_inv(self.params_mean, self.params_std)

    def compile_observables(self):
        """Creates observables dict"""
        self.observables = {}
        self.observables['loss.lprobs'] = self.network.lprobs
        for p in self.network.aps:
            self.observables[str(p)] = p

    def monitor_dict_from_names(self, monitor=None):
        """Generate monitor dict from list of variable names"""
        if monitor is not None:
            observe = {}
            if isinstance(monitor, str):
                monitor = [monitor]
            for m in monitor:
                if m in self.observables:
                    observe[m] = self.observables[m]
        else:
            observe = None
        return observe
