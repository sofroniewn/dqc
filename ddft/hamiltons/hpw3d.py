import torch
import numpy as np
from ddft.hamiltons.base_hamilton import BaseHamilton

class HamiltonPW3D(BaseHamilton):
    def __init__(self, rgrid, boxshape):
        # rgrid is (nr,3), ordered by (x, y, z)
        # boxshape (3,) = (nx, ny, nz)
        super(HamiltonPW3D, self).__init__()

        # set up the space
        self.space = QSpace(rgrid, boxshape)
        self.qgrid = self.space.qgrid # (ns)
        self.q2 = (self.qgrid*self.qgrid).expand(-1,2) # (ns,2)

        self._rgrid = rgrid
        self._boxshape = boxshape
        self.ndim = len(boxshape)
        nr = rgrid.shape[0]

        # get the pixel size
        self.pixsize = rgrid[1,:] - rgrid[0,:] # (3,)
        self.dr3 = torch.prod(self.pixsize)
        self.inv_dr3 = 1.0 / self.dr3

        # check the shape
        if torch.prod(boxshape) != nr:
            msg = "The product of boxshape elements must be equal to the "\
                  "first dimension of rgrid"
            raise ValueError(msg)

        # prepare the diagonal part of kinetics
        self.Kdiag = torch.ones(nr).to(rgrid.dtype).to(rgrid.device) * self.ndim # (nr,)

    def kinetics(self, wf):
        # wf: (nbatch, nr, ncols)
        # wf consists of points in the real space

        # perform the operation in q-space, so FT the wf first
        wfT = wf.transpose(-2, -1) # (nbatch, ncols, nr)
        coeff = self.space.transformsig(wfT, dim=-1) # (nbatch, ncols, ns, 2)
        # wfT = self.boxifysig(wfT, dim=-1) # (nbatch, ncols, nx, ny, nz)
        # coeff = torch.rfft(wfT, signal_ndim=3) # (nbatch, ncols, nx, ny, nz, 2)

        # multiply with |q|^2 and IFT transform it back
        coeffq2 = coeff * self.q2 # (nbatch, ncols, ns, 2)
        kin = self.space.invtransformsig(coeffq2, dim=-2) # (nbatch, ncols, nr)

        # revert to the original shape
        return kin.transpose(-2, -1) # (nbatch, nr, ncols)

    def kinetics_diag(self, nbatch):
        return self.Kdiag.unsqueeze(0).expand(nbatch,-1) # (nbatch, nr)

    def getdens(self, eigvec2):
        return eigvec2 * self.inv_dr3

    def integralbox(self, p, dim=-1):
        return p.sum(dim=dim) * self.dr3