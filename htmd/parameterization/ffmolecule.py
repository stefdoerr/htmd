# (c) 2015-2017 Acellera Ltd http://www.acellera.com
# All Rights Reserved
# Distributed under HTMD Software License Agreement
# No redistribution in whole or part
#
from htmd.molecule.molecule import Molecule
from htmd.molecule.vmdparser import guessAnglesAndDihedrals
from htmd.parameterization.detectsoftdihedrals import detectSoftDihedrals
from htmd.parameterization.detectequivalents import detectEquivalents
from htmd.parameterization.fftype import FFTypeMethod, FFType
from htmd.parameterization.ff import RTF, PRM
from htmd.parameterization.ffevaluate import FFEvaluate
from htmd.qm import Psi4
from htmd.parameterization.phi import setPhi, getPhi
from htmd.parameterization.esp import ESP
from htmd.progress.progress import ProgressBar
import re
import math
from math import cos
import os
import scipy.optimize as optimize
import numpy as np
from copy import deepcopy

class QMFittingSet:
    # phi_coords  = []
    def __init__(self):
        self.phi = []
        self.qm = []
        self.mm_original = []
        self.mm_zeroed = []
        self.mm_delta = []
        self.mm_fitted = []
        self.coords = []


class FFMolecule(Molecule):
    def __init__(self, filename=None, name=None, rtf=None, prm=None, netcharge=None, method=FFTypeMethod.CGenFF_2b6,
                 qm=None, outdir="./"):
        # filename -- a mol2 format input geometry
        # rtf, prm -- rtf, prm files
        # method  -- if rtf, prm == None, guess atom types according to this method ( of enum FFTypeMethod )
        self.method = method
        self.outdir = outdir

        if not (filename.endswith(".mol2")):
            raise ValueError("Input file must be mol2 format")

        super().__init__(filename=filename, name=name)

        if(len(self.bonds)==0):
           print("No bonds found. Guessing them")
           self.bonds =  self._guessBonds()
        (a, b) = guessAnglesAndDihedrals(self.bonds)
        self.natoms = self.serial.shape[0]
        self.angles = a
        self.dihedrals = b
        ee = detectEquivalents(self)
        self._soft_dihedrals = detectSoftDihedrals(self, ee)
        self._equivalent_atom_groups = ee[0]  # list of groups of equivalent atoms
        self._equivalent_atoms = ee[1]  # list of equivalent atoms, indexed by atom
        self._equivalent_group_by_atom = ee[2]  # mapping from atom index to equivalent atom group
        if netcharge is None:
            self.netcharge = int(round(np.sum(self.charge)))
        else:
            self.netcharge = int(round(netcharge))

        # Canonicalise the atom naming.
        self._rename_mol()

        if rtf and prm:
            # If the user has specified explicit RTF and PRM files go ahead and load those
            self._rtf = RTF(rtf)
            self._prm = PRM(prm)
        elif method == FFTypeMethod.NONE:
            # Don't assign any atom types
            pass
        else:
            # Otherwise make atom types using the specified method
            fftype = FFType(self, method=self.method)
            self._rtf = fftype._rtf
            self._prm = fftype._prm

        self.qm = qm if qm else Psi4()

        self.theory_name = self.qm.theory
        self.basis_name = self.qm.basis
        self.basis_name = re.sub('\+', 'plus', self.basis_name)  # Replace '+' with 'plus'
        self.basis_name = re.sub('\*', 'star', self.basis_name)  # Replace '*' with 'star'
        self.solvent_name = self.qm.solvent.lower()

        if hasattr(self, '_rtf'):
            self.impropers = np.array(self._rtf.impropers)

        self.report()

    def copy(self):

        # HACK! Circumvent 'qm' coping problem
        qm, self.qm = self.qm, None
        copy = super().copy()
        self.qm = copy.qm = qm

        return copy

    def report(self):
        print("Net Charge: {}".format(self.netcharge))
        print("Equivalent atom groups:")
        for i in self._equivalent_atom_groups:
            for j in i:
                print(" {}".format(self.name[j]), end="")
            print("")

        print("Soft torsions:")
        for i in self._soft_dihedrals:
            for j in i.atoms:
                print(" {}".format(self.name[j]), end="")
            print("")

    def _rename_mol(self):
        # This fixes up the atom naming and reside name to be consistent
        # NB this scheme matches what MATCH does. Don't change it
        # Or the naming will be inconsistent with the RTF
        import re

        hh = dict()

        for i in range(len(self.name)):
            # This fixes the specific case where a name is 3 characters, as X-TOOL seems to make
            t = self.name[i].upper()
            if re.match( "^[A-Z][A-Z][A-Z]", t ): 
               t = t[0]
            # Remove any character that isn't alpha
            t = re.sub('[^A-Z]*', "", t )
            if t != self.name[i].upper():
#               print(" Atom #%d renamed from [%s] to [%s]" %(i, self.name[i], t ) )
               pass

            idx = 0

            if not t in hh:
                hh[t] = idx

            idx = hh[t] + 1
            hh[t] = idx

            t += str(idx)
            self.name[i] = t
            self.resname[i] = "MOL"

    def output_directory_name(self):
        return self.theory_name + "-" + self.basis_name + "-" + self.solvent_name 

    def minimize(self):
        mindir = os.path.join(self.outdir, "minimize", self.output_directory_name())
        try:
            os.makedirs(mindir, exist_ok=True)
        except:
            raise OSError('Directory {} could not be created. Check if you have permissions.'.format(mindir))

        self.qm.molecule = self
        self.qm.esp_points = None
        self.qm.optimize = True
        self.qm.restrained_dihedrals = None
        self.qm.directory = mindir
        results = self.qm.run()
        if results[0].errored:
            raise RuntimeError("QM Optimization failed")

        # Replace coordinates with the minimized set
        self.coords = np.atleast_3d(results[0].coords)

    def _fitDihedral_objective(self, x):
        inv = math.pi / 180.

        # evaluate the torsion with the input params
        # for each of the phi's poses
        chisq = 0.
        for t in range(self._fitDihedral_results.N):
            e = .0  # FFEvaluate.evaluateTorsion( self._fitDihedral_results["phi_coords"][t], phi )
            for s in range(len(self._fitDihedral_results.phis[t])):
                for j in range(6):
                    e += x[j] * (1. + cos((j + 1) * (self._fitDihedral_results.phis[t][s] * inv) - x[6 + j] * inv))

            e = e + x[12]
            diff = self._fitDihedral_results.mm_delta[t] - e
            chisq += diff * diff

        return chisq

    def removeCOM(self):
        '''Relocate centre of mass to the origin'''

        masses = np.array([self._rtf.mass_by_type[self._rtf.type_by_index[i]] for i in range(self.natoms)])
        for frame in range(self.numFrames):
            self.coords[:, :, frame] -= np.dot(masses, self.coords[:, :, frame])/np.sum(masses)

    def fitCharges(self, fixed=[]):

        # Remove the COM from the coords, because PSI4 does it and then the grid is incorrectly centred
        self.removeCOM()

        # Cereate an ESP directory
        espDir = os.path.join(self.outdir, "esp", self.output_directory_name() )
        os.makedirs(espDir, exist_ok=True)

        # Get ESP points
        point_file = os.path.join(espDir, "00000", "grid.dat")
        if os.path.exists(point_file):
            # Load a point file if one exists from a previous job
            esp_points = np.loadtxt(point_file)
            print("Reusing previously-generated point cloud")
        else:
            # Generate ESP points
            esp_points = ESP.generate_points(self)[0]

        # Run QM simulation
        self.qm.molecule = self
        self.qm.esp_points = esp_points
        self.qm.optimize = False
        self.qm.restrained_dihedrals = None
        self.qm.directory = espDir
        qm_results = self.qm.run()
        if qm_results[0].errored:
            raise RuntimeError("QM Calculation failed")
        self.coords = qm_results[0].coords

        # Fit ESP charges
        self.esp = ESP()
        self.esp.molecule = self
        self.esp.qm_results = qm_results
        self.esp.fixed = fixed
        esp_result = self.esp.run()
        esp_charges, esp_loss = esp_result['charges'], esp_result['loss']

        # Update the charges
        self.charge[:] = esp_charges
        self._rtf.updateCharges(esp_charges)

        return esp_loss, qm_results[0].dipole

    def getDipole(self):
        """Calculate the dipole moment (in Debyes) of the molecule"""

        coords = self.coords[:, :, self.frame]
        coords -= np.mean(coords, axis=0)

        dipole = np.zeros(4)
        dipole[:3] = np.dot(self.charge, coords)
        dipole[3] = np.linalg.norm(dipole[:3]) # Total dipole moment
        dipole /= 0.20819434 # e * Ang --> Debye (https://en.wikipedia.org/wiki/Debye)

        return dipole

    def getSoftTorsions(self):
        dd = []
        for d in self._soft_dihedrals:
            dd.append(d.atoms.copy())
        return dd

    #  def scanSoftDihedral(self, phi, directory = "dihedral", step=10):
    #    found=False
    #    phi_to_fit = None
    #    frozens=[]
    #    dih_index=0
    #    i=0
    #    for d in self._soft_dihedrals:
    #      if (d.atoms == phi).all():
    #         phi_to_fit = d
    #         dih_index=i
    #         frozens.append(d.atoms)
    #      else:
    #         pass
    #      i=i+1
    #    if not phi_to_fit: raise ValueError( "specified phi is not a recognised soft dihedral" )
    #
    #    atoms = phi_to_fit.atoms
    #    left  = phi_to_fit.left
    #    right = phi_to_fit.right
    #    equivs= phi_to_fit.equivalents
    #
    ##    step  = 10 # degrees
    #    nstep = (int)(360/step)
    #    cset  = np.zeros( ( self.natoms, 3, nstep ) )
    #
    #    i=0
    #    for phi in range( -180, 180, step ):
    #      cset[:,:,i] = setPhi( self.coords[:,:,0], atoms, left, right, phi )
    #      i=i+1
    #
    #    mol        = self.copy()
    #    mol.coords = cset
    #    try:
    #      os.mkdir( directory )
    #    except:
    #      pass
    #    dih_name = "%s-%s-%s-%s" % ( self.name[atoms[0]], self.name[atoms[1]], self.name[atoms[2]], self.name[atoms[3]] )
    #    qmset   = QMCalculation( mol, charge=self.netcharge, directory="%s/%s" % (directory, dih_name), frozen=frozens, optimized=True )
    #    r = qmset.results()
    #    x=0
    #    ret=[]
    #    for phi in range( -180, 180, step ):
    #      r[x].phi = phi
    #      if r[x].errored == False:
    #        ret.append(r[x])
    #      x=x+1
    #    return ret

    def fitSoftTorsion(self, phi, geomopt=True):
        found = False
        phi_to_fit = None
        frozens = []
        dih_index = 0
        i = 0
        bkp_coords = self.coords.copy()

        for d in self._soft_dihedrals:
            if (d.atoms == phi).all():
                phi_to_fit = d
                dih_index = i
                frozens.append(d.atoms)
            else:
                if not geomopt:
                    frozens.append(d.atoms)
            i += 1
        if not phi_to_fit:
            raise ValueError("specified phi is not a recognised soft dihedral")
        self._makeDihedralUnique(phi_to_fit)

        atoms = phi_to_fit.atoms
        left = phi_to_fit.left
        right = phi_to_fit.right
        equivs = phi_to_fit.equivalents

        step = 10  # degrees
        nstep = int(360 / step)
        cset = np.zeros((self.natoms, 3, nstep))

        i = 0
        for phi in range(-180, 180, step):
            cset[:, :, i] = setPhi(self.coords[:, :, 0], atoms, left, right, phi)
            i += 1

        mol = self.copy()
        mol.coords = cset

        dirname = 'dihedral-opt' if geomopt else 'dihedral-single-point'
        dih_name = "%s-%s-%s-%s" % (self.name[atoms[0]], self.name[atoms[1]], self.name[atoms[2]], self.name[atoms[3]])
        fitdir = os.path.join(self.outdir, dirname, dih_name, self.output_directory_name())

        try:
            os.makedirs(fitdir, exist_ok=True)
        except:
            raise OSError('Directory {} could not be created. Check if you have permissions.'.format(fitdir))

        self.qm.molecule = mol
        self.qm.esp_points = None
        self.qm.optimize = geomopt
        self.qm.restrained_dihedrals = np.array(frozens)
        self.qm.directory = fitdir
        results = self.qm.run()

        ret = self._makeDihedralFittingSetFromQMResults(atoms, results)

        # Get the initial parameters of the dihedral we are going to fit

        param = self._prm.dihedralParam(self._rtf.type_by_index[atoms[0]],
                                        self._rtf.type_by_index[atoms[1]],
                                        self._rtf.type_by_index[atoms[2]],
                                        self._rtf.type_by_index[atoms[3]])

        # Save these parameters as the best fit (fit to beat)
        best_param = np.zeros((13))
        for t in range(6):
            best_param[t] = param[t].k0
            best_param[t + 6] = param[t].phi0
        best_param[12] = 0.

        #    print(param)

        # Evalaute the mm potential with this dihedral zeroed out
        # The objective function will try to fit to the delta between
        # The QM potential and the this modified mm potential

        for t in param:
            t.k0 = t.phi0 = 0.
            #t.e14 = 1.  # Use whatever e14 has been inherited for the type
        self._prm.updateDihedral(param)

        ffeval = FFEvaluate(self)
        #  print(ffe.evaluate( ret.coords[0] ) )
        #  input
        # Now evaluate the ff without the dihedral being fitted
        for t in range(ret.N):
            mm_zeroed = ffeval.run(ret.coords[t][:, :, 0])['total']
            ret.mm_delta.append(ret.qm[t] - mm_zeroed)
            ret.mm_zeroed.append(mm_zeroed)

        mmin1 = min(ret.mm_zeroed)
        mmin2 = min(ret.mm_delta)
        for t in range(ret.N):
            ret.mm_zeroed[t] = ret.mm_zeroed[t] - mmin1
            ret.mm_delta[t] = ret.mm_delta[t] - mmin2

        self._fitDihedral_results = ret
        self._fitDihedral_phi = param

        # Now measure all of the soft dihedrals phis that are mapped to this dihedral
        ret.phis = []
        for i in range(ret.N):
            ret.phis.append([ret.phi[i]])
            for e in equivs:
                ret.phis[i].append(getPhi(ret.coords[i], e))
            #    print ("EQUIVALENT DIHEDRALS FOR THIS DIHEDRAL" )
            #    print(equivs)
            #    print ("PHI VALUES TO FIT")
            #    print (ret.phis)
        # Set up the NOLOPT fit
        #  There are 13 parameters, k,phi for n=1,2,3,4,5,6 and a shift
        N = 13
        # initial guess,
        st = np.zeros(13)
        # bounds

        best_chisq = self._fitDihedral_objective(best_param)
        #    print("CHISQ of initial = %f" % ( best_chisq ) )

        # Now zero out the terms of the dihedral we are going to fit
        bar = ProgressBar(64, description="Fitting")
        for i in range(64):

            (bounds, start) = self._fitDihedral_make_bounds(i)

            xopt = optimize.minimize(self._fitDihedral_objective, start, method="L-BFGS-B", bounds=bounds,
                                     options={'disp': False})

            chisq = self._fitDihedral_objective(xopt.x)
            #      print( "CHISQ of fit = %f " % (chisq) )
            if (chisq < best_chisq):
                best_chisq = chisq
                best_param = xopt.x
            bar.progress()
        bar.stop()
        #    print("Best ChiSQ = %f" %(best_chisq) )

        # Update the target dihedral with the optimized parameters
        # print(param)
        # print(best_param )
        for i in range(6):
            param[i].k0 = best_param[0 + i]
            param[i].phi0 = best_param[6 + i]

        self._prm.updateDihedral(param)
        # print(param)
        param = self._prm.dihedralParam(self._rtf.type_by_index[atoms[0]],
                                        self._rtf.type_by_index[atoms[1]],
                                        self._rtf.type_by_index[atoms[2]],
                                        self._rtf.type_by_index[atoms[3]])
        # print(param)

        # Finally evaluate the fitted potential
        ffeval = FFEvaluate(self)
        for t in range(ret.N):
            ret.mm_fitted.append(ffeval.run(ret.coords[t][:, :, 0])['total'])
        mmin = min(ret.mm_fitted)
        chisq = 0.

        #    print( "QM energies" )
        #    print( ret.qm )

        for t in range(ret.N):
            ret.mm_fitted[t] = ret.mm_fitted[t] - mmin
            delta = ret.mm_fitted[t] - ret.qm[t]
            chisq = chisq + (delta * delta)
        ret.chisq = chisq

        # TODO Score it
        self.coords = bkp_coords

        return ret

    def _fitDihedral_make_bounds(self, i):
        lb = np.zeros(13)
        ub = np.zeros(13)
        start = np.zeros(13)

        bounds = []

        for j in range(6):
            start[j] = 0.
            bounds.append((-20., 20.))

        for j in range(6):
            if i & (2 ** j):
                bounds.append((180., 180.))
                start[6 + j] = 180.
            else:
                bounds.append((0., 0.))
                start[6 + j] = 0.

        bounds.append((-10., 10.))
        return bounds, start

    def _makeDihedralFittingSetFromQMResults(self, atoms, results):
        # Extract the valid QM poses and energies from the QM result set
        # Evaluate the MM on those poses
        ffeval = FFEvaluate(self)

        ret = QMFittingSet()
        ret.name = "%s-%s-%s-%s" % (
            self._rtf.names[atoms[0]], self._rtf.names[atoms[1]], self._rtf.names[atoms[2]], self._rtf.names[atoms[3]])

        qmin = 1.e100
        for q in results:
            if not q.errored:
                if q.energy < qmin:
                    qmin = q.energy

        completed = 0
        for q in results:
            if not q.errored:
                if (q.energy - qmin) < 20.:  # Only fit against QM points < 20 kcal above the minimum
                    mmeval = ffeval.run(q.coords[:, :, 0])
                    if mmeval["vdw"] < 200:
                        completed += 1
                        phi = getPhi(q.coords, atoms)

                        ret.qm.append(q.energy - qmin)
                        ret.mm_original.append(mmeval['total'])
                        ret.coords.append(q.coords)
                        if phi > 180.:
                            phi -= 360.
                        ret.phi.append(phi)
                    else:
                        print("Omitting optimised pose for phi=%f (MM VDW too high)" % phi)
                else:
                    print("Omitting optimised QM pose (QM energy too high %f)" % q.energy)

        mmin = min(ret.mm_original)
        # roughly align the qm with the mm
        for q in range(completed):
            ret.mm_original[q] = ret.mm_original[q] - mmin
        ret.N = completed

        if completed < 5:
            raise RuntimeError("Fewer than 5 valid QM points. Not enough to fit!")

        return ret

    def _makeDihedralUnique(self, phi_to_fit):
        #    (number_of_uses, uses) = self._countUsesOfDihedral( phi_to_fit.atoms )
        #    if( number_of_uses > 1 ):
        # Create a new type for (arbitrarily) a middle atom of the dihedral
        # So that the dihedral we are going to modify is unique
        # TODO -- check symmetry
        #    print( "Dihedral term is not unique. Copying type.." ) # Used %d times, by:" % ( number_of_uses ) )
        # print( uses )

        # Duplicate the dihedrals types so this modified term is unique
#        print("Duplicating types..")
        for i in range(4):
            if not ("x" in self._rtf.type_by_index[phi_to_fit.atoms[i]]):
                self._duplicateTypeOfAtom(phi_to_fit.atoms[i])

        (number_of_uses, uses) = self._countUsesOfDihedral(phi_to_fit.atoms)
        if number_of_uses > 1:
            print(phi_to_fit.atoms)
            print(number_of_uses)
            print(uses)
            raise ValueError("Dihedral term still not unique after duplication")

    def _countUsesOfDihedral(self, aidx):

        # Return the number of uses of the dihedral
        # specified by the types of the 4 atom indices in the aidx list
        #

        #    print( "countUsesOfDihedral in " )
        t1 = self._rtf.type_by_index[aidx[0]]
        t2 = self._rtf.type_by_index[aidx[1]]
        t3 = self._rtf.type_by_index[aidx[2]]
        t4 = self._rtf.type_by_index[aidx[3]]

        count = 0
        uses = []
        for d in self.dihedrals:
            s1 = self._rtf.type_by_index[d[0]]
            s2 = self._rtf.type_by_index[d[1]]
            s3 = self._rtf.type_by_index[d[2]]
            s4 = self._rtf.type_by_index[d[3]]
            if s1 == t1 and s2 == t2 and s3 == t3 and s4 == t4:
                count += 1
                uses.append(d)
            elif s1 == t4 and s2 == t3 and s3 == t2 and s4 == t1:
                count += 1
                uses.append(d)
            #    return(count, uses )
            #    print(uses)

        # Now for each of the uses, remove any which are equivalent
        c = 1
        unique_uses = [aidx]
        g1 = self._equivalent_group_by_atom[aidx[0]]
        g2 = self._equivalent_group_by_atom[aidx[1]]
        g3 = self._equivalent_group_by_atom[aidx[2]]
        g4 = self._equivalent_group_by_atom[aidx[3]]
        for u in uses:
            h1 = self._equivalent_group_by_atom[u[0]]
            h2 = self._equivalent_group_by_atom[u[1]]
            h3 = self._equivalent_group_by_atom[u[2]]
            h4 = self._equivalent_group_by_atom[u[3]]
            equiv = False
            if g1 == h1 and g2 == h2 and g3 == h3 and g4 == h4:
                equiv = True
            if g1 == h4 and g2 == h3 and g3 == h2 and g4 == h1:
                equiv = True
            if equiv is False:
                c += 1
                unique_uses.append(u)
            else:
#                print(" Dih %s-%s-%s-%s and %s-%s-%s-%s are equivalent " % (
#                    self._rtf.names[aidx[0]], self._rtf.names[aidx[1]], self._rtf.names[aidx[2]],
#                    self._rtf.names[aidx[3]], self._rtf.names[u[0]], self._rtf.names[u[1]], self._rtf.names[u[2]],
#                    self._rtf.names[u[3]]))
                pass
                #  return(count, uses )
            #    print( c )
            #    print( unique_uses )
        return c, unique_uses

    def _duplicateTypeOfAtom(self, aidx):
        # This duplicates the type of the specified atom
        # First get the type
        atype = self._rtf.type_by_index[aidx]

        # perhaps the type is already a duplicate? if so
        # remove the duplicated suffix
        atype = re.sub("x[0123456789]+$", "", atype)
        i = 0
        # make the new type name
        while ("%sx%d" % (atype, i)) in self._rtf.types:
            i += 1

        newtype = "%sx%d" % (atype, i)
        print("Creating new type %s from %s for atom %s" % (newtype, atype, self._rtf.names[aidx]))

        # duplicate the type in the fields RTF -- todo: move to a method in the RTF
        self._rtf.type_by_index[aidx] = newtype
        self._rtf.mass_by_type[newtype] = self._rtf.mass_by_type[atype]
        self._rtf.types.append(newtype)
        self._rtf.type_by_name[self._rtf.names[aidx]] = newtype
        self._rtf.type_by_index[aidx] = newtype
        self._rtf.typeindex_by_type[newtype] = self._rtf.typeindex_by_type[atype] + 1000
        self._rtf.element_by_type[newtype] = self._rtf.element_by_type[atype]

        #    # Now also reset the type of  any atoms that share equivalency
        for bidx in self._equivalent_atoms[aidx]:
            if aidx != bidx:
                if "x" in self._rtf.type_by_index[bidx]:
                    raise RuntimeError(
                        "Equivalent atom already has a duplicated type: {} {}".format(bidx, self._rtf.type_by_index[bidx]))
                self._rtf.type_by_index[bidx] = newtype
                self._rtf.type_by_name[self._rtf.names[bidx]] = newtype

        # the PRM parameters will be automatically duplicated by forcing an ff evaluation
        FFEvaluate(self).run(self.coords[:, :, 0])

    def plotConformerEnergies( self, fits, show=True ):
        import matplotlib as mpl
        if not show:
            mpl.use('Agg')
        import matplotlib.pyplot as plt

        fh = plt.figure()
        ax1 = fh.gca()
     
        if( len(fits) == 0 ): return
  
        mm_energy = []
        qm_energy = []
        for r in fits:
            mm_energy.extend(r.mm_fitted)
            qm_energy.extend(r.qm)
        qm_energy = np.array(qm_energy)
        mm_energy = np.array(mm_energy)

        qm_energy = qm_energy - min(qm_energy)
        mm_energy = mm_energy - min(mm_energy)

        qm_energy = qm_energy.reshape(qm_energy.shape[0], 1)
        mm_energy = mm_energy.reshape(mm_energy.shape[0], 1)
#        print(qm_energy)
#        print(qm_energy.shape)
#        print(mm_energy)
#        print(mm_energy.shape)
        from sklearn import linear_model
        regr = linear_model.LinearRegression(fit_intercept=False)
        regr.fit(qm_energy, mm_energy)

        ax1.set_xlabel("QM Energy kcal/mol")
        ax1.set_xlabel("MM Energy kcal/mol")
        ax1.set_title("Conformer Energies  MM vs QM")
        ax1.plot(qm_energy, mm_energy,  color="black", marker="o", linestyle="None")
        ax1.plot(qm_energy, regr.predict(qm_energy), color="red", linewidth=2)

        if show:
            plt.show()
        else:
            plotdir = os.path.join(self.outdir, "parameters", self.method.name, self.output_directory_name(), "plots")
            try:
                os.makedirs(plotdir, exist_ok=True)
            except:
                raise OSError('Directory {} could not be created. Check if you have permissions.'.format(plotdir))
            tf = os.path.join(plotdir, "conformer-energies.svg" ) 
            plt.savefig(tf, format="svg")

        # Return RMS error, variance and fit coeffients
        return (
          np.mean((regr.predict(qm_energy) - mm_energy)**2),
          regr.score(qm_energy, mm_energy),
          regr.coef_
        )

    def plotTorsionFit(self, fit, phi_original, show=True):
        import matplotlib as mpl
        if not show:
            mpl.use('Agg')
        import matplotlib.pyplot as plt

        fh = plt.figure()
        ax1 = fh.gca()
        ax1.set_xlim(-180., 180.)
        ax1.set_xticks([-180, -135, -90, -45, 0, 45, 90, 135, 180])
        ax1.set_xlabel("Phi")
        ax1.set_ylabel("kcal/mol")
        ax1.set_title(fit.name)

        x = sorted(fit.phi)
        plotdata = []
        for i in range(len(fit.phi)):
            plotdata.append((fit.phi[i], fit.qm[i]))
        plotdata = sorted(plotdata)
        plotdatax = [float(i[0]) for i in plotdata]
        plotdatay = [float(i[1]) for i in plotdata]
        ax1.plot(plotdatax, plotdatay, label="QM", color="r", marker="o")

        plotdata=[]
        for i in range(len(phi_original)):
            plotdata.append((phi_original[i], fit.mm_original[i]))
        plotdata = sorted(plotdata)
        plotdatax = [float(i[0]) for i in plotdata]
        plotdatay = [float(i[1]) for i in plotdata]
        ax1.plot(plotdatax, plotdatay, label="MM Original", color="b", marker="d")

        plotdata=[]
        for i in range(len(fit.phi)):
            plotdata.append((fit.phi[i], fit.mm_fitted[i]))
        plotdata = sorted(plotdata)
        plotdatax = [float(i[0]) for i in plotdata]
        plotdatay = [float(i[1]) for i in plotdata]
        ax1.plot(plotdatax, plotdatay, label="MM Fitted", color="g", marker="s")

        #ax1.plot(fit.phi, fit.qm, label="QM", color="r", marker="o")
        #ax1.plot(fit.phi, fit.mm_original, label="MM Original", color="b", marker="d")
        #ax1.plot(fit.phi, fit.mm_fitted, label="MM Fitted", color="g", marker="s")
        ##    ax1.plot( fit.phi , fit.mm_zeroed  , label="MM With phi zeroed", color="black", marker="x" )
        ##    ax1.plot( fit.phi , fit.mm_delta   , label="QM-MM target", color="magenta", marker="x" )
        ax1.legend(prop={'size': 8})
        if show:
            plt.show()
        else:
            plotdir = os.path.join(self.outdir, "parameters", self.method.name, self.output_directory_name(), "plots")
            try:
                os.makedirs(plotdir, exist_ok=True)
            except:
                raise OSError('Directory {} could not be created. Check if you have permissions.'.format(plotdir))
            tf = os.path.join(plotdir, fit.name) + ".svg"
            plt.savefig(tf, format="svg")
            plt.clf()
            return tf

    def write(self, filename, sel=None, type=None, typemap=None):
        if hasattr(self, "_rtf"):  # Update base Molecule's attributes so write() works correctly
            for i in range(self.charge.shape[0]):
                self.segid[i] = "L"
                self.charge[i] = self._rtf.charge_by_name[self.name[i]]
                self.atomtype[i] = self._rtf.type_by_name[self.name[i]]

        ref_atomtype = deepcopy(self.atomtype)
        if typemap:
            for i in range(self.charge.shape[0]):
                self.atomtype[i] = typemap[self.atomtype[i]]

        super().write(filename, sel=sel, type=type)

        self.atomtype = ref_atomtype
