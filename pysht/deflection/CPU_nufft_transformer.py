import os
import numpy as np
import healpy as hp

import line_profiler

import ducc0
import finufft

from lenspyx.lensing import get_geom as lenspyx_get_geom
from lenspyx.remapping import deflection as lenspyx_deflection
from lenspyx.utils_hp import Alm, alm2cl, almxfl, alm_copy
from lenspyx.remapping.utils_angles import d2ang

import pysht
from pysht import cacher
from pysht.utils import timer
import pysht.geometry as geometry
from pysht.geometry import Geom
from pysht.helper import shape_decorator, timing_decorator, debug_decorator
from pysht.sht.CPU_sht_transformer import CPU_SHT_DUCC_transformer, CPU_SHT_SHTns_transformer

ctype = {np.dtype(np.float32): np.complex64,
         np.dtype(np.float64): np.complex128,
         np.dtype(np.longfloat): np.longcomplex,
         np.float32: np.complex64,
         np.float64: np.complex128,
         np.longfloat: np.longcomplex}
rtype = {np.dtype(np.complex64): np.float32,
         np.dtype(np.complex128): np.float64,
         np.dtype(np.longcomplex): np.longfloat,
         np.complex64: np.float32,
         np.complex128: np.float64,
         np.longcomplex: np.longfloat}

HAS_DUCCPOINTING = 'get_deflected_angles' in ducc0.misc.__dict__
HAS_DUCCROTATE = 'lensing_rotate' in ducc0.misc.__dict__
HAS_DUCCGRADONLY = 'mode:' in ducc0.sht.experimental.synthesis.__doc__

if HAS_DUCCPOINTING:
    from ducc0.misc import get_deflected_angles
if HAS_DUCCROTATE:
    from ducc0.misc import lensing_rotate
try:
    from lenspyx.fortran.remapping import remapping as fremap
    HAS_FORTRAN = True
except:
    HAS_FORTRAN = False

@staticmethod
def ducc_sht_mode(gclm, spin):
    gclm_ = np.atleast_2d(gclm)
    return 'GRAD_ONLY' if ((gclm_[0].size == gclm_.size) * (abs(spin) > 0)) else 'STANDARD'

class deflection:
    def __init__(self, dlm, mmax_dlm:int, geom, dclm:np.ndarray=None, epsilon=1e-5, verbosity=0, nthreads=10, single_prec=True, planned=False):
        self.single_prec = single_prec
        self.verbosity = verbosity
        self.tim = timer(verbose=self.verbosity)
        self.planned = False
        self._cis = False
        self.cacher = cacher.cacher_mem()
        self.epsilon = epsilon
        self.nthreads=nthreads
        
        dlm = np.atleast_2d(dlm)
        self.dlm = dlm
        
        self.lmax_dlm = Alm.getlmax(dlm[0].size, mmax_dlm)
        self.mmax_dlm = mmax_dlm
        
        s2_d = np.sum(alm2cl(dlm[0], dlm[0], self.lmax_dlm, mmax_dlm, self.lmax_dlm) * (2 * np.arange(self.lmax_dlm + 1) + 1)) / (4 * np.pi)
        if dlm.shape[0]>1:
            s2_d += np.sum(alm2cl(dlm[1], dlm[1], self.lmax_dlm, mmax_dlm, self.lmax_dlm) * (2 * np.arange(self.lmax_dlm + 1) + 1)) / (4 * np.pi)
            s2_d /= np.sqrt(2.)
        sig_d = np.sqrt(s2_d / geom.fsky())
        sig_d_amin = sig_d / np.pi * 180 * 60
        if sig_d >= 0.01:
            print('deflection std is %.2e amin: this is really too high a value for something sensible'%sig_d_amin)
        elif self.verbosity:
            print('deflection std is %.2e amin' % sig_d_amin)
            

    def set_nufftgeometry(self, geom_desc):
        self.nufftgeom = geometry.get_geom(geom_desc)
        self.set_geometry(geom_desc)
        
    def flip_tpg_2d(self, m):
        # FIXME this should probably be lmax, not lmax_dlm
        # dim of m supposedly (2, -1)
        buff = np.array([_.reshape(2*(self.lmax_dlm+1),-1).T.flatten() for _ in m])
        return buff

    def _build_d1(self, dlm, lmax_dlm, mmax_dlm, dclm=None):
        '''
        # FIXME this is a bit of a mess, this function should not distinguish between different SHT backends.
        # Instead, there should be a _build_d1() for each, and they should sit in the repsective transformer modules.
        
        This depends on the backend. If SHTns, we can use the synthesis_der1 method. If not, we use a spin-1 SHT
        '''
        ll = np.arange(0,lmax_dlm+1,1)
        if self.shttransformer_desc == 'shtns':
            if dclm is None:
                synth_spin1_map = self.synthesis_der1(hp.almxfl(dlm, np.nan_to_num(np.sqrt(1/(ll*(ll+1))))), nthreads=self.nthreads)
                
            else:
                assert 0, "implement if needed, not sure if this is possible with SHTns"
                # FIXME: want to do that only once
                dgclm = np.empty((2, dlm.size), dtype=dlm.dtype)
                dgclm[0] = dlm
                dgclm[1] = dclm
                synth_spin1_map = self.synthesis_der1(hp.almxfl(dlm, np.nan_to_num(np.sqrt(1/(ll*(ll+1))))), nthreads=self.nthreads)
            return self.flip_tpg_2d(synth_spin1_map)
        elif self.shttransformer_desc == 'ducc':
            if dclm is None:
                # undo p2d to use
                d1 = self.synthesis(dlm, spin=1, lmax=lmax_dlm, mmax=mmax_dlm, nthreads=self.nthreads, mode='GRAD_ONLY')
            else:
                # FIXME: want to do that only once
                dgclm = np.empty((2, dlm.size), dtype=dlm.dtype)
                dgclm[0] = dlm
                dgclm[1] = dclm
                d1 = self.synthesis(dgclm, spin=1, lmax=lmax_dlm, mmax=mmax_dlm, nthreads=self.nthreads)
            return d1
        elif self.shttransformer_desc == 'pysht':
            assert 0, "implement if needed"
        else:
            assert 0, "Not sure what to do with {}".format(self.shttransformer_desc)

    # @profile
    def _build_angles(self, dlm, lmax_dlm, mmax_dlm, fortran=True, calc_rotation=True):
        """Builds deflected positions and angles

            Returns (npix, 3) array with new tht, phi and -gamma

        """
        fns = ['ptg'] + calc_rotation * ['gamma']
        if not np.all([self.cacher.is_cached(fn) for fn in fns]) :
            d1 = self._build_d1(dlm, lmax_dlm, mmax_dlm)
            self.timer.add('spin-1 maps')
            # Probably want to keep red, imd double precision for the calc?
            if HAS_DUCCPOINTING:
                tht, phi0, nph, ofs = self.geom.theta, self.geom.phi0, self.geom.nph, self.geom.ofs
                tht_phip_gamma = get_deflected_angles(theta=tht, phi0=phi0, nphi=nph, ringstart=ofs, deflect=d1.T,
                                                        calc_rotation=calc_rotation, nthreads=self.nthreads)
                if calc_rotation:
                    self.cacher.cache(fns[0], tht_phip_gamma[:, 0:2])
                    self.cacher.cache(fns[1], tht_phip_gamma[:, 2] if not self.single_prec else tht_phip_gamma[:, 2].astype(np.float32))
                else:
                    self.cacher.cache(fns[0], tht_phip_gamma)
                return
            npix = self.geom.npix()
            thp_phip_gamma = np.empty((3, npix), dtype=float)  # (-1) gamma in last arguement
            startpix = 0
            assert np.all(self.geom.theta > 0.) and np.all(self.geom.theta < np.pi), 'fix this (cotangent below)'
            red, imd = d1
            for ir in np.argsort(self.geom.ofs): # We must follow the ordering of scarf position-space map
                pixs = Geom.rings2pix(self.geom, [ir])
                if pixs.size > 0:
                    t_red = red[pixs]
                    i_imd = imd[pixs]
                    phis = Geom.phis(self.geom, ir)[pixs - self.geom.ofs[ir]]
                    assert phis.size == pixs.size, (phis.size, pixs.size)
                    thts = self.geom.theta[ir] * np.ones(pixs.size)
                    thtp_, phip_ = d2ang(t_red, i_imd, thts , phis, int(np.round(np.cos(self.geom.theta[ir]))))
                    sli = slice(startpix, startpix + len(pixs))
                    thp_phip_gamma[0, sli] = thtp_
                    thp_phip_gamma[1, sli] = phip_
                    cot = np.cos(self.geom.theta[ir]) / np.sin(self.geom.theta[ir])
                    d = np.sqrt(t_red ** 2 + i_imd ** 2)
                    thp_phip_gamma[2, sli] = np.arctan2(i_imd, t_red ) - np.arctan2(i_imd, d * np.sin(d) * cot + t_red * np.cos(d))
                    startpix += len(pixs)
            self.cacher.cache(fns[0], thp_phip_gamma.T[:, 0:2])
            if calc_rotation:
                self.cacher.cache(fns[1], thp_phip_gamma.T[:, 2] if not self.single_prec else thp_phip_gamma.T[:, 2].astype(np.float32) )
            assert startpix == npix, (startpix, npix)
            return


    def _get_ptg(self, dlm, mmax):
        # TODO improve this and fwd angles, e.g. this is computed twice for gamma if no cacher
        self._build_angles(dlm, mmax, mmax) if not self._cis else self._build_angleseig()
        return self.cacher.load('ptg')


class CPU_finufft_transformer(deflection):
    def __init__(self, shttransformer_desc, geominfo, deflection_kwargs):
        self.backend = 'CPU'
        self.shttransformer_desc = shttransformer_desc
        if shttransformer_desc == 'ducc':
            self.BaseClass = type('CPU_SHT_DUCC_transformer()', (CPU_SHT_DUCC_transformer,), {})
            self.instance = self.BaseClass(geominfo)
        elif shttransformer_desc == 'shtns':
            self.BaseClass = type('CPU_SHT_SHTns_transformer()', (CPU_SHT_SHTns_transformer,), {})
            self.instance = self.BaseClass(geominfo)
        else:
            raise ValueError('shttransformer_desc must be either "ducc" or "shtns"')

        self.geominfo = geominfo
        self.set_geometry(geominfo)
        if 'mmax' in geominfo[1]:
            del geominfo[1]['mmax']
        self.nufftgeom = geometry.get_geom(geominfo)
        deflection_kwargs.update({'geom':self.nufftgeom})    
        super().__init__(**deflection_kwargs)


    def __getattr__(self, name):
        return getattr(self.instance, name)


    def set_nufftgeometry(self, geom_desc):
        self.nufftgeom = geometry.get_geom(geom_desc)
        self.set_geometry(geom_desc)

    # @profile
    def gclm2lenmap(self, gclm, dlm, lmax, mmax, spin, nthreads, polrot=True, cc_transformer=None, HAS_DUCCPOINTING=True, mode=0):
        """CPU algorithm for spin-n remapping using finufft
            Args:
                gclm: input alm array, shape (ncomp, nalm), where ncomp can be 1 (gradient-only) or 2 (gradient or curl)
                mmax: mmax parameter of alm array layout, if different from lmax
                spin: spin (>=0) of the transform
                backwards: forward or backward (adjoint) operation
        """ 
        if mode == 0:
            print('Running in normal mode')
            timing = False
            debug = False
        if mode == 1:
            print('Running in timing mode')
            timing = True
            debug = False
        if mode == 2:
            print("Running in debug mode")
            timing = False
            debug = True
        ret = []
        
        self.timer = timer(1, prefix=self.backend)
        self.timer.start('gclm2lenmap()')
        gclm = np.atleast_2d(gclm)
        lmax_unl = Alm.getlmax(gclm[0].size, mmax)
        if mmax is None:
            mmax = lmax_unl
        if self.single_prec and gclm.dtype != np.complex64:
            gclm = gclm.astype(np.complex64)
        # self.timer.add('setup')

        # transform slm to Clenshaw-Curtis map
        if not debug:
            ntheta = (ducc0.fft.good_size(lmax_unl + 2) + 3) // 4 * 4
            # ntheta = ducc0.fft.good_size(lmax_unl + 2)
            nphihalf = ducc0.fft.good_size(lmax_unl + 1)
            nphi = 2 * nphihalf
        else:
            ntheta = lmax+1
            nphihalf = lmax+1
            nphi = 2 * nphihalf
        # self.timer.add('params')
        
        ### SYNTHESIS CC GEOMETRY ###
        mode = ducc_sht_mode(gclm, spin)
        map = ducc0.sht.experimental.synthesis_2d(alm=gclm, ntheta=ntheta, nphi=nphi, spin=spin, lmax=lmax_unl, mmax=mmax, geometry="CC", nthreads=nthreads, mode=mode)
        self.timer.add('synthesis')
        if debug:
            ret.append(np.copy(map))
        
        map_dfs = np.empty((2 * ntheta - 2, nphi), dtype=np.complex128 if spin == 0 else ctype[map.dtype])
        if spin == 0:
            map_dfs[:ntheta, :] = map[0]
        else:
            map_dfs[:ntheta, :].real = map[0]
            map_dfs[:ntheta, :].imag = map[1]
        del map
        map_dfs[ntheta:, :nphihalf] = map_dfs[ntheta - 2:0:-1, nphihalf:]
        map_dfs[ntheta:, nphihalf:] = map_dfs[ntheta - 2:0:-1, :nphihalf]
        if (spin % 2) != 0:
            map_dfs[ntheta:, :] *= -1
        self.timer.add('doubling')
        if debug:
            ret.append(np.copy(map_dfs))


        # go to Fourier space
        if spin == 0:
            tmp = np.empty(map_dfs.shape, dtype=np.complex128)
            map_dfs = ducc0.fft.c2c(map_dfs, axes=(0, 1), inorm=2, nthreads=nthreads, out=tmp)
            del tmp
        else:
            map_dfs = ducc0.fft.c2c(map_dfs, axes=(0, 1), inorm=2, nthreads=nthreads, out=map_dfs)
        self.timer.add('c2c')
        if debug:
            ret.append(np.copy(map_dfs))
        
        if self.planned: # planned nufft
            assert ptg is None
            plan = self.make_plan(lmax_unl, spin)
            values = plan.u2nu(grid=map_dfs, forward=False, verbosity=self.verbosity)
            self.tim.add('planned u2nu')
        else:
            ptg = None
            if ptg is None:
                ptg = self._get_ptg(dlm, mmax)
            self.timer.add('get ptg')
            if debug:
                ret.append(np.copy(ptg))
                
            map_shifted = np.fft.fftshift(map_dfs, axes=(0,1))
            x_ = np.array(ptg[:,0], order="C")
            y_ = np.array(ptg[:,1], order="C")
            f_ = np.array(map_shifted, dtype=np.complex128, order="C")
            v_ = finufft.nufft2d2(x=x_, y=y_, f=f_, isign=1)
            self.timer.add('nuFFT')
            values = np.roll(np.real(v_))
            
        if debug:
            ret.append(np.copy(values))   

        if polrot * spin:
            if self._cis:
                cis = self._get_cischi()
                for i in range(polrot * abs(spin)):
                    values *= cis
                self.tim.add('polrot (cis)')
            else:
                if HAS_DUCCROTATE:
                    lensing_rotate(values, self._get_gamma(), spin, nthreads)
                    self.tim.add('polrot (ducc)')
                else:
                    func = fremap.apply_inplace if values.dtype == np.complex128 else fremap.apply_inplacef
                    func(values, self._get_gamma(), spin, nthreads)
                    self.tim.add('polrot (fortran)')
        if debug:
            ret.append(np.copy(values)) 
        
        if timing:
            self.timer.dumpjson('/mnt/home/sbelkner/git/pySHT/test/benchmark/timings/CPU_finufft_{}'.format(lmax))
        if debug:
            return ret
        else:
            return values.real.flatten() if spin == 0 else values.view(rtype[values.dtype]).reshape((values.size, 2)).T
        # np.atleast_2d(values.real.flatten())


    def lenmap2gclm(self, points:np.ndarray[complex or float], dlm, spin:int, lmax:int, mmax:int, nthreads:int, gclm_out=None, sht_mode='STANDARD'):
        """
            Note:
                points mst be already quadrature-weigthed
                For inverse-lensing, need to feed in lensed maps times unlensed forward magnification matrix.
        """
        self.tim.reset()
        if spin == 0 and not np.iscomplexobj(points):
            points = points.astype(ctype[points.dtype]).squeeze()
        if spin > 0 and not np.iscomplexobj(points):
            points = (points[0] + 1j * points[1]).squeeze()
        # FIXME stop passing synthesis function as _get_d1 needs it..
        ptg = self._get_ptg(dlm, mmax)


        ntheta = ducc0.fft.good_size(lmax + 2)
        nphihalf = ducc0.fft.good_size(lmax + 1)
        nphi = 2 * nphihalf
        map_dfs = np.empty((2 * ntheta - 2, nphi), dtype=points.dtype)
        if self.planned:
            plan = self.make_plan(lmax, spin)
            map_dfs = plan.nu2u(points=points, out=map_dfs, forward=True, verbosity=self.verbosity)

        else:
            # perform NUFFT
        
            map_dfs = ducc0.nufft.nu2u(points=points, coord=ptg, out=map_dfs, forward=True,
                                       epsilon=self.epsilon, nthreads=nthreads, verbosity=self.verbosity,
                                       periodicity=2 * np.pi, fft_order=True)
        # go to position space
        map_dfs = ducc0.fft.c2c(map_dfs, axes=(0, 1), forward=False, inorm=2, nthreads=nthreads, out=map_dfs)

        # go from double Fourier sphere to Clenshaw-Curtis grid
        if (spin % 2) != 0:
            map_dfs[1:ntheta - 1, :nphihalf] -= map_dfs[-1:ntheta - 1:-1, nphihalf:]
            map_dfs[1:ntheta - 1, nphihalf:] -= map_dfs[-1:ntheta - 1:-1, :nphihalf]
        else:
            map_dfs[1:ntheta - 1, :nphihalf] += map_dfs[-1:ntheta - 1:-1, nphihalf:]
            map_dfs[1:ntheta - 1, nphihalf:] += map_dfs[-1:ntheta - 1:-1, :nphihalf]
        map_dfs = map_dfs[:ntheta, :]
        map = np.empty((1 if spin == 0 else 2, ntheta, nphi), dtype=rtype[points.dtype])
        map[0] = map_dfs.real
        if spin > 0:
            map[1] = map_dfs.imag
        del map_dfs

        # adjoint SHT synthesis
        slm = ducc0.sht.experimental.adjoint_synthesis_2d(map=map, spin=spin, lmax=lmax, mmax=mmax, geometry="CC", nthreads=nthreads, mode=sht_mode, alm=gclm_out)
        return slm.squeeze()
    

    def lensgclm(self, gclm:np.ndarray, dlm:np.array, spin:int, lmax_out:int, nthreads:int, mmax:int=None, mmax_out:int=None,gclm_out:np.ndarray=None, polrot=True, out_sht_mode='STANDARD'):
        """Adjoint remapping operation from lensed alm space to unlensed alm space

            Args:
                gclm: input gradient and possibly curl mode ((1 or 2, nalm)-shaped complex numpy.ndarray)
                mmax: set this for non-standard mmax != lmax in input array
                spin: spin-weight of the fields (larger or equal 0)
                lmax_out: desired output array lmax
                mmax_out: desired output array mmax (defaults to lmax_out if None)
                gclm_out(optional): output array (can be same as gclm provided it is large enough)
                polrot(optional): includes small rotation of spin-weighted fields (defaults to True)
                out_sht_mode(optional): e.g. 'GRAD_ONLY' if only the output gradient mode is desired
            Note:
                 nomagn=True is a backward comptability thing to ask for inverse lensing
        """
        stri = 'lengclm ' +  'fwd' 
        self.tim.start(stri)
        self.tim.reset()
        input_sht_mode = ducc_sht_mode(gclm, spin)
        if mmax_out is None:
            mmax_out = lmax_out
        m = self.gclm2lenmap(gclm, dlm=dlm, lmax=lmax_out, mmax=lmax_out, spin=spin, nthreads=nthreads, polrot=polrot)
        self.tim.reset()
        if gclm_out is not None:
            assert gclm_out.dtype == ctype[m.dtype], 'type precision must match'
        gclm_out = self.adjoint_synthesis(m, spin=spin, lmax=lmax_out, mmax=mmax_out, nthreads=nthreads, alm=gclm_out, mode=out_sht_mode)
        return gclm_out.squeeze()

    
    def synthesis_general(self, lmax, mmax, map, loc, spin, epsilon, nthreads, sht_mode, alm, verbose):
        assert 0, "implement if needed"
        return synthesis_general(lmax=lmax, mmax=mmax, alm=alm, loc=loc, spin=spin, epsilon=self.epsilon, nthreads=self.sht_tr, mode=sht_mode, verbose=self.verbosity)
    
    def adjoint_synthesis_general(self, lmax, mmax, map, loc, spin, epsilon, nthreads, sht_mode, alm, verbose):
        assert 0, "implement if needed"
        return adjoint_synthesis_general(lmax=lmax, mmax=mmax, map=map, loc=loc, spin=spin, epsilon=self.epsilon, nthreads=self.sht_tr, mode=sht_mode, alm=alm, verbose=self.verbosity)


class CPU_DUCCnufft_transformer(deflection):
    def __init__(self, shttransformer_desc, geominfo, deflection_kwargs):
        self.backend = 'CPU'
        self.shttransformer_desc = shttransformer_desc
        if shttransformer_desc == 'ducc':
            self.BaseClass = type('CPU_SHT_DUCC_transformer()', (CPU_SHT_DUCC_transformer,), {})
            self.instance = self.BaseClass(geominfo)
        elif shttransformer_desc == 'shtns':
            self.BaseClass = type('CPU_SHT_SHTns_transformer()', (CPU_SHT_SHTns_transformer,), {})
            self.instance = self.BaseClass(geominfo)
        else:
            raise ValueError('shttransformer_desc must be either "ducc" or "shtns"')
        
        self.geominfo = geominfo
        self.set_geometry(geominfo)
        if 'mmax' in geominfo[1]:
            del geominfo[1]['mmax']
        self.nufftgeom = geometry.get_geom(geominfo)
        deflection_kwargs.update({'geom':self.nufftgeom})    
        super().__init__(**deflection_kwargs)

    def __getattr__(self, name):
        return getattr(self.instance, name)

    def set_nufftgeometry(self, geom_desc):
        self.nufftgeom = geometry.get_geom(geom_desc)
        self.set_geometry(geom_desc)

    @timing_decorator
    @debug_decorator
    def dlm2pointing(self, dlm, mmax, pointing_theta, pointing_phi):
        pointing_theta, pointing_phi =  self._get_ptg(dlm, mmax).T
        return tuple([pointing_theta, pointing_phi])

    def gclm2lenmap(self, gclm, dlm, lmax, mmax, spin, nthreads, polrot=True, pointing_theta=None, pointing_phi=None, mode=0):
        """CPU algorithm for spin-n remapping using duccnufft
            Args:
                gclm: input alm array, shape (ncomp, nalm), where ncomp can be 1 (gradient-only) or 2 (gradient or curl)
                mmax: mmax parameter of alm array layout, if different from lmax
                spin: spin (>=0) of the transform
                backwards: forward or backward (adjoint) operation
        """ 

        s2_d = np.sum(alm2cl(dlm, dlm, lmax, mmax, lmax) * (2 * np.arange(lmax + 1) + 1)) / (4 * np.pi)
        sig_d = np.sqrt(s2_d / self.geom.fsky())
        sig_d_amin = sig_d / np.pi * 180 * 60
        if sig_d >= 0.01:
            print('deflection std is %.2e amin: this is really too high a value for something sensible'%sig_d_amin)
        elif self.verbosity:
            print('deflection std is %.2e amin' % sig_d_amin)     
        self.timer = timer(1, prefix=self.backend)
        self.timer.start('gclm2lenmap()')
        self.ret = {}
            
        @timing_decorator
        def _setup(self, gclm, lmax, mmax, mode):
            if mode == 0:
                print('Running in normal mode')
                timing = False
                debug = False
            if mode == 1:
                print('Running in timing mode')
                timing = True
                debug = False
            if mode == 2:
                print("Running in debug mode")
                timing = False
                debug = True

            gclm = np.atleast_2d(gclm)
            lmax_unl = Alm.getlmax(gclm[0].size, mmax)
            if mmax is None:
                mmax = lmax_unl
            if self.single_prec and gclm.dtype != np.complex64:
                gclm = gclm.astype(np.complex64)

            if True: #not debug:
                # FIXME this only works if CAR grid is initialized with good fft size, otherwise this clashes with doubling
                # ntheta = (ducc0.fft.good_size(lmax_unl + 2) + 3) // 4 * 4
                ntheta = ducc0.fft.good_size(lmax_unl + 2)
                nphihalf = ducc0.fft.good_size(lmax_unl + 1)
                nphi = 2 * nphihalf
            else:
                ntheta = lmax+1
                nphihalf = lmax+1
                nphi = 2 * nphihalf
            self.timing = timing
            self.debug = debug
            print("ntheta: ", ntheta, "nphihalf: ", nphihalf, "nphi: ", nphi, "lmax_unl: ", lmax_unl, "mmax: ", mmax)
            return gclm, lmax, lmax_unl, mmax, ntheta, nphihalf, nphi
         
        @debug_decorator
        @timing_decorator
        @shape_decorator
        def _synthesis(self, gclm, out):
            out = ducc0.sht.experimental.synthesis_2d(alm=gclm, ntheta=ntheta, nphi=nphi, spin=spin, lmax=lmax_unl, mmax=mmax, geometry="CC", nthreads=nthreads, mode=ducc_sht_mode(gclm, spin))
            return tuple([out])
        
        @debug_decorator
        @timing_decorator
        @shape_decorator
        def _doubling(self, map, ntheta, nphi, out):
            map_dfs = np.empty((2 * ntheta - 2, nphi), dtype=map.dtype if spin == 0 else ctype[map.dtype])
            if spin == 0:
                map_dfs[:ntheta, :] = map[0]
            else:
                map_dfs[:ntheta, :].real = map[0]
                map_dfs[:ntheta, :].imag = map[1]
            del map
            map_dfs[ntheta:, :nphihalf] = map_dfs[ntheta - 2:0:-1, nphihalf:]
            map_dfs[ntheta:, nphihalf:] = map_dfs[ntheta - 2:0:-1, :nphihalf]
            if (spin % 2) != 0:
                map_dfs[ntheta:, :] *= -1
            return tuple([map_dfs])
        
        @debug_decorator
        @timing_decorator
        @shape_decorator
        def _C2C(self, map_dfs, spin, out):
            if spin == 0:
                tmp = np.empty(map_dfs.shape, dtype=ctype[map_dfs.dtype])
                map_dfs = ducc0.fft.c2c(map_dfs.copy(), axes=(0, 1), inorm=2, nthreads=nthreads, out=tmp, forward=True)
                del tmp
            else:
                map_dfs = ducc0.fft.c2c(map_dfs, axes=(0, 1), inorm=2, nthreads=nthreads, out=map_dfs)
            return tuple([map_dfs])
        
        @debug_decorator
        @timing_decorator
        @shape_decorator
        def _nuFFT(self, map_dfs, theta, phi, out):
            out = ducc0.nufft.u2nu(grid=map_dfs.T, coord=np.array([phi,theta]).T, forward=False, epsilon=self.epsilon, nthreads=self.nthreads, verbosity=self.verbosity, periodicity=2*np.pi, fft_order=True)
            return tuple([out])
        
        @debug_decorator
        @timing_decorator
        @shape_decorator
        def _rotate(self, lenmap):
            if polrot * spin:
                if self._cis:
                    cis = self._get_cischi()
                    for i in range(polrot * abs(spin)):
                        lenmap *= cis
                else:
                    if HAS_DUCCROTATE:
                        lensing_rotate(lenmap, self._get_gamma(), spin, nthreads)
                    else:
                        func = fremap.apply_inplace if lenmap.dtype == np.complex128 else fremap.apply_inplacef
                        func(lenmap, self._get_gamma(), spin, nthreads)
            return tuple([lenmap])
             
        self.timing, self.debug = None, None
        gclm, lmax, lmax_unl, mmax, ntheta, nphihalf, nphi = _setup(self, gclm, lmax, mmax, mode)       
        if pointing_theta is None or pointing_phi is None:
            pointing_theta = np.zeros(self.geom.npix(), dtype=np.double)
            pointing_phi = np.zeros(self.geom.npix(), dtype=np.double)
            pointing_theta, pointing_phi = self.dlm2pointing(dlm, mmax, pointing_theta, pointing_phi)
            
        out = None
        map = _synthesis(self, gclm, out)[0]
        out = None
        map_dfs = _doubling(self, map, ntheta, nphi, out)[0]
        out = None
        map_dfs = _C2C(self, map_dfs, spin, out)[0]
        lenmap = _nuFFT(self, map_dfs, pointing_theta, pointing_phi, out)[0]
        lenmap = _rotate(self, lenmap)[0]
        
        if self.timing:
            print(self.timer)
            self.timer.dumpjson(os.path.dirname(pysht.__file__)[:-5]+'/test/benchmark/timings/CPU_duccnufft_{}'.format(lmax))
        if self.debug:
            return self.ret
        else:
            return lenmap.real if spin == 0 else lenmap.view(rtype[lenmap.dtype]).reshape((lenmap.size, 2)).T


    def lenmap2gclm(self, points:np.ndarray[complex or float], dlm:np.ndarray, spin:int, lmax:int, mmax:int, nthreads:int, gclm_out=None, sht_mode='STANDARD'):
        """
            Note:
                points mst be already quadrature-weigthed
                For inverse-lensing, need to feed in lensed maps times unlensed forward magnification matrix.
        """
        self.tim.reset()
        if spin == 0 and not np.iscomplexobj(points):
            points = points.astype(ctype[points.dtype]).squeeze()
        if spin > 0 and not np.iscomplexobj(points):
            points = (points[0] + 1j * points[1]).squeeze()
        ptg = self._get_ptg(dlm, mmax)

        ntheta = ducc0.fft.good_size(lmax + 2)
        nphihalf = ducc0.fft.good_size(lmax + 1)
        nphi = 2 * nphihalf
        map_dfs = np.empty((2 * ntheta - 2, nphi), dtype=points.dtype)
        if self.planned:
            plan = self.make_plan(lmax, spin)
            map_dfs = plan.nu2u(points=points, out=map_dfs, forward=True, verbosity=self.verbosity)

        else:
            # perform NUFFT
            map_dfs = ducc0.nufft.nu2u(points=points, coord=ptg, out=map_dfs, forward=True,
                                       epsilon=self.epsilon, nthreads=nthreads, verbosity=self.verbosity,
                                       periodicity=2 * np.pi, fft_order=True)
        # go to position space
        map_dfs = ducc0.fft.c2c(map_dfs, axes=(0, 1), forward=False, inorm=2, nthreads=nthreads, out=map_dfs)

        # go from double Fourier sphere to Clenshaw-Curtis grid
        if (spin % 2) != 0:
            map_dfs[1:ntheta - 1, :nphihalf] -= map_dfs[-1:ntheta - 1:-1, nphihalf:]
            map_dfs[1:ntheta - 1, nphihalf:] -= map_dfs[-1:ntheta - 1:-1, :nphihalf]
        else:
            map_dfs[1:ntheta - 1, :nphihalf] += map_dfs[-1:ntheta - 1:-1, nphihalf:]
            map_dfs[1:ntheta - 1, nphihalf:] += map_dfs[-1:ntheta - 1:-1, :nphihalf]
        map_dfs = map_dfs[:ntheta, :]
        map = np.empty((1 if spin == 0 else 2, ntheta, nphi), dtype=rtype[points.dtype])
        map[0] = map_dfs.real
        if spin > 0:
            map[1] = map_dfs.imag
        del map_dfs

        # adjoint SHT synthesis
        slm = ducc0.sht.experimental.adjoint_synthesis_2d(map=map, spin=spin, lmax=lmax, mmax=mmax, geometry="CC", nthreads=nthreads, mode=sht_mode, alm=gclm_out)
        return slm.squeeze()
    
    
    def lensgclm(self, gclm:np.ndarray, dlm:np.array, spin:int, lmax_out:int, nthreads:int, mmax:int=None, mmax_out:int=None,gclm_out:np.ndarray=None, polrot=True, out_sht_mode='STANDARD'):
        """Adjoint remapping operation from lensed alm space to unlensed alm space

            Args:
                gclm: input gradient and possibly curl mode ((1 or 2, nalm)-shaped complex numpy.ndarray)
                mmax: set this for non-standard mmax != lmax in input array
                spin: spin-weight of the fields (larger or equal 0)
                lmax_out: desired output array lmax
                mmax_out: desired output array mmax (defaults to lmax_out if None)
                gclm_out(optional): output array (can be same as gclm provided it is large enough)
                backwards: forward or adjoint (not the same as inverse) lensing operation
                polrot(optional): includes small rotation of spin-weighted fields (defaults to True)
                out_sht_mode(optional): e.g. 'GRAD_ONLY' if only the output gradient mode is desired


            Note:
                 nomagn=True is a backward comptability thing to ask for inverse lensing


        """
        stri = 'lengclm ' +  'fwd' 
        self.tim.start(stri)
        self.tim.reset()
        input_sht_mode = ducc_sht_mode(gclm, spin)
        if mmax_out is None:
            mmax_out = lmax_out
        m = self.gclm2lenmap(gclm, dlm=dlm, lmax=lmax_out, mmax=lmax_out, spin=spin, nthreads=nthreads, polrot=polrot)
        self.tim.reset()
        if gclm_out is not None:
            assert gclm_out.dtype == ctype[m.dtype], 'type precision must match'
        gclm_out = self.adjoint_synthesis(m, spin=spin, lmax=lmax_out, mmax=mmax_out, nthreads=nthreads, alm=gclm_out, mode=out_sht_mode)
        return gclm_out.squeeze()
    
        
    def synthesis_general(self, lmax, mmax, map, loc, spin, epsilon, nthreads, sht_mode, alm, verbose):
        assert 0, "implement if needed"
        return synthesis_general(lmax=lmax, mmax=mmax, alm=alm, loc=loc, spin=spin, epsilon=self.epsilon, nthreads=self.sht_tr, mode=sht_mode, verbose=self.verbosity)

  
    def adjoint_synthesis_general(self, lmax, mmax, map, loc, spin, epsilon, nthreads, sht_mode, alm, verbose):
        assert 0, "implement if needed"
        return adjoint_synthesis_general(lmax=lmax, mmax=mmax, map=map, loc=loc, spin=spin, epsilon=self.epsilon, nthreads=self.sht_tr, mode=sht_mode, alm=alm, verbose=self.verbosity)

class CPU_Lenspyx_transformer:
    def __init__(self, shttransformer_desc, geominfo, deflection_kwargs):
        self.shttransformer_desc = shttransformer_desc
        # FIXME propagate mmax
        self.lenspyx = lenspyx_deflection(lenspyx_get_geom(geominfo), deflection_kwargs['dlm'], geominfo[1]['lmax'], numthreads=deflection_kwargs['nthreads'], verbosity=deflection_kwargs['verbosity'], epsilon=deflection_kwargs['epsilon'], single_prec=deflection_kwargs['single_prec'])
        self.backend = 'CPU'
        # self.lenspyx = self.lenspyx.change_dlm([deflection_kwargs['dlm'], None], mmax_dlm=deflection_kwargs['mmax_dlm'])
        
    def gclm2lenmap(self, gclm:np.ndarray, dlm, lmax, mmax:int or None, spin:int, nthreads, backwards:bool=False, polrot=True, ptg=None, epsilon=1e-8, single_prec=True, dclm=None, mode=0):
        # FIXME check incoming lmax/mmax passing, and nthreads
        self.timing, self.debug = None, None
        def _setup(self, mode):
            if mode == 0:
                print('Running in normal mode')
                self.timing = False
                self.debug = False
            if mode == 1:
                print('Running in timing mode')
                self.timing = True
                self.debug = False
            if mode == 2:
                print("Running in debug mode")
                self.timing = False
                self.debug = True
            return self.timing, self.debug
        self.timer = timer(1, prefix=self.backend)
        self.timer.start('lenspyx()')
        self.timing, self.debug = _setup(self, mode)
        res = self.lenspyx.gclm2lenmap(gclm=gclm, backwards=backwards, mmax=mmax, spin=spin)
        self.timer.add('gclm2lenmap')
        if self.timing:
            self.timer.dumpjson(os.path.dirname(pysht.__file__)[:-5]+'/test/benchmark/timings/CPU_lenspyx_{}'.format(lmax))
            print(self.timer)
            print("::timing:: stored new timing data")

    def lenmap2gclm(self, points:np.ndarray[complex or float], dlm:np.ndarray, spin:int, lmax:int, mmax:int, nthreads:int, gclm_out=None, sht_mode='STANDARD'):
        print("geom name: {}".format(self.lenspyx.geom.name))
        return self.lenspyx.lenmap2gclm(points=np.atleast_2d(points), spin=spin, lmax=lmax, mmax=mmax, gclm_out=gclm_out, sht_mode=sht_mode)
    
    def lensgclm(self, gclm:np.ndarray, dlm:np.array, spin:int, lmax_out:int, nthreads:int, mmax:int=None, mmax_out:int=None,gclm_out:np.ndarray=None, polrot=True, out_sht_mode='STANDARD'):
        return self.lenspyx.lensgclm(gclm=gclm, mmax=mmax, spin=spin, lmax_out=lmax_out, mmax_out=mmax_out)
   
    def synthesis_general(self, lmax, mmax, map, loc, spin, epsilon, nthreads, sht_mode, alm, verbose):
        assert 0, "implement if needed"
        return synthesis_general(lmax=lmax, mmax=mmax, alm=alm, loc=loc, spin=spin, epsilon=self.epsilon, nthreads=self.sht_tr, mode=sht_mode, verbose=self.verbosity)
    
    def adjoint_synthesis_general(self, lmax, mmax, map, loc, spin, epsilon, nthreads, sht_mode, alm, verbose):
        assert 0, "implement if needed"
        return adjoint_synthesis_general(lmax=lmax, mmax=mmax, map=map, loc=loc, spin=spin, epsilon=self.epsilon, nthreads=self.sht_tr, mode=sht_mode, alm=alm, verbose=self.verbosity)