import numpy as np

from delfi.kernel.BaseKernel import BaseKernel


class Gauss(BaseKernel):
    @staticmethod
    def kernel(u):
        return 1/np.sqrt(2*np.pi)*np.exp(-0.5*u**2)
        
class HalfGauss(BaseKernel):
    def __init__(self, obs, bandwidth=1., atleast=None):
      super().__init__(obs, bandwidth=bandwidth, spherical=False, atleast=atleast)

    @staticmethod
    def kernel(u):
        if u <= 0.0 :
            return 1.0
        else:
            return 1/np.sqrt(2*np.pi)*np.exp(-0.5*u**2)
