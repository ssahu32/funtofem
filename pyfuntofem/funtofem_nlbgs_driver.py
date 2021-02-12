#!/usr/bin/env python
"""
This file is part of the package FUNtoFEM for coupled aeroelastic simulation
and design optimization.

Copyright (C) 2015 Georgia Tech Research Corporation.
Additional copyright (C) 2015 Kevin Jacobson, Jan Kiviaho and Graeme Kennedy.
All rights reserved.

FUNtoFEM is licensed under the Apache License, Version 2.0 (the "License");
you may not use this software except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from __future__ import print_function

from .funtofem_driver import *

class FUNtoFEMnlbgs(FUNtoFEMDriver):
    def __init__(self,solvers,comm,struct_comm,struct_master,aero_comm,aero_master,transfer_options=None,model=None,
                 theta_init=0.125,theta_min=0.01,theta_max=1.0):
        """
        The FUNtoFEM driver for the Nonlinear Block Gauss-Seidel solvers for steady and unsteady coupled adjoint.

        Parameters
        ----------
        solvers: dict
           the various disciplinary solvers
        comm: MPI.comm
            MPI communicator
        transfer_options: dict
            options of the load and displacement transfer scheme
        model: :class:`~funtofem_model.FUNtoFEMmodel`
            The model containing the design data
        theta_init: float
            Initial value of theta for the Aitken under-relaxation
        theta_min: float
            Minimum value of theta for the Aitken under-relaxation
        """

        super(FUNtoFEMnlbgs,self).__init__(solvers,comm,struct_comm,struct_master,aero_comm,aero_master,transfer_options=transfer_options,model=model)

        # Aitken acceleration settings
        self.theta_init = theta_init
        self.theta_min = theta_min
        self.theta_max = theta_max
        self.theta = []

        self.aitken_init = None
        self.aitken_vec = None
        self.up_prev = None

    def _initialize_adjoint_variables(self,scenario,bodies):
        """
        Initialize the adjoint variables

        Parameters
        ----------
        scenario: :class:`~scenario.Scenario`
            The scenario
        bodies: :class:`~body.Body`
            List of FUNtoFEM bodies.
        """
        nfunctions = scenario.count_adjoint_functions()
        nfunctions_total = len(scenario.functions)

        for body in bodies:
            body.psi_L = np.zeros((body.struct_nnodes*body.xfer_ndof,nfunctions),
                                  dtype=TransferScheme.dtype)
            body.psi_S = np.zeros((body.struct_nnodes*body.xfer_ndof,nfunctions),
                                  dtype=TransferScheme.dtype)
            body.struct_rhs = np.zeros((body.struct_nnodes*body.xfer_ndof,nfunctions),
                                       dtype=TransferScheme.dtype)

            body.dLdfa = np.zeros((body.aero_nnodes*3,nfunctions),
                                  dtype=TransferScheme.dtype)
            body.dGdua = np.zeros((body.aero_nnodes*3,nfunctions),
                                  dtype=TransferScheme.dtype)
            body.psi_D = np.zeros((body.aero_nnodes*3,nfunctions),
                                  dtype=TransferScheme.dtype)

            if body.shape:
                body.aero_shape_term = np.zeros((body.aero_nnodes*3,nfunctions_total),dtype=TransferScheme.dtype)
                body.struct_shape_term = np.zeros((body.struct_nnodes*body.xfer_ndof,nfunctions_total),dtype=TransferScheme.dtype)


    def _solve_steady_forward(self,scenario,steps=None):
        """
        Solve the aeroelastic forward analysis using the nonlinear block Gauss-Seidel algorithm.
        Aitken under-relaxation for stabilty.

        Parameters
        ----------
        scenario: :class:`~scenario.Scenario`
            The current scenario
        steps: int
            Number of iterations if not set by the model
        """

        self.aitken_init = True
        fail = 0

        # Determine if we're using the scenario's number of steps or the argument
        if steps is None:
            if self.model:
                steps = scenario.steps
            else:
                if self.comm.Get_rank()==0:
                    print("No number of steps given for the coupled problem. Using default (1000)")
                steps = 1000

        # Loop over the NLBGS steps
        for step in range(1,steps+1):
            # Transfer displacements
            for body in self.model.bodies:
                body.aero_disps = np.zeros(body.aero_nnodes*3,dtype=TransferScheme.dtype)
                body.transfer.transferDisps(body.struct_disps, body.aero_disps)

            # Take a step in the flow solver
            fail = self.solvers['flow'].iterate(scenario,self.model.bodies,step)

            fail = self.comm.allreduce(fail)
            if fail != 0:
                if self.comm.Get_rank() == 0:
                    print('Flow solver returned fail flag')
                return fail

            # Transfer the loads
            for body in self.model.bodies:
                body.struct_loads = np.zeros(body.struct_nnodes*body.xfer_ndof,dtype=TransferScheme.dtype)
                body.transfer.transferLoads(body.aero_loads, body.struct_loads)

            # Take a step in the FEM model
            fail = self.solvers['structural'].iterate(scenario,self.model.bodies,step)

            fail = self.comm.allreduce(fail)
            if fail != 0:
                if self.comm.Get_rank() == 0:
                    print('Structural solver returned fail flag')
                return fail

            # Under-relaxation for solver stability
            self._aitken_relax()


        # end solve loop
        return fail

    def _solve_steady_adjoint(self,scenario):
        """
        Solve the aeroelastic adjoint analysis using the linear block Gauss-Seidel algorithm.
        Aitken under-relaxation for stabilty.

        Parameters
        ----------
        scenario: :class:`~scenario.Scenario`
            The current scenario
        """
        fail = 0
        self.aitken_init = True

        # how many steps to take
        steps = scenario.steps

        # Load the current state
        for body in self.model.bodies:
            aero_disps = np.zeros(body.aero_disps.size,dtype=TransferScheme.dtype)
            body.transfer.transferDisps(body.struct_disps, aero_disps)

            struct_loads = np.zeros(body.struct_loads.size,dtype=TransferScheme.dtype)
            body.transfer.transferLoads(body.aero_loads, struct_loads)

        # Initialize the adjoint variables
        nfunctions = scenario.count_adjoint_functions()
        self._initialize_adjoint_variables(scenario,self.model.bodies)

        # loop over the adjoint NLBGS solver
        for step in range(1,steps+1):
            # Get force terms for the flow solver
            for body in self.model.bodies:
                for func in range(nfunctions):
                    # 'Solve' for load transfer adjoint variables
                    body.psi_L[:,func] = body.psi_S[:,func]

                    # Transform load transfer adjoint variables using transpose Jacobian from
                    # funtofem: dLdfA^T * psi_L = dDdus * psi_L
                    psi_L_r = np.zeros(body.aero_nnodes*3,dtype=TransferScheme.dtype)
                    body.transfer.applydDduS(body.psi_L[:, func].copy(order='C'), psi_L_r)
                    body.dLdfa[:,func] = psi_L_r

            fail = self.solvers['flow'].iterate_adjoint(scenario,self.model.bodies,step)

            fail = self.comm.allreduce(fail)
            if fail != 0:
                if self.comm.Get_rank() == 0:
                    print('Flow solver returned fail flag')
                return fail

            # Get the structural adjoint rhs
            for body in self.model.bodies:
                for func in range(nfunctions):

                    # calculate dDdu_s^T * psi_D
                    psi_D_product = np.zeros(body.struct_nnodes*body.xfer_ndof,dtype=TransferScheme.dtype)
                    body.psi_D = - body.dGdua
                    body.transfer.applydDduSTrans(body.psi_D[:, func].copy(order='C'), psi_D_product)

                    # calculate dLdu_s^T * psi_L
                    psi_L_product = np.zeros(body.struct_nnodes*body.xfer_ndof,dtype=TransferScheme.dtype)
                    body.transfer.applydLduSTrans(body.psi_L[:, func].copy(order='C'), psi_L_product)

                    body.struct_rhs[:,func] = -psi_D_product - psi_L_product

            # take a step in the structural adjoint
            fail = self.solvers['structural'].iterate_adjoint(scenario,self.model.bodies,step)

            fail = self.comm.allreduce(fail)
            if fail != 0:
                if self.comm.Get_rank() == 0:
                    print('Structural solver returned fail flag')
                return fail
            self._aitken_adjoint_relax(scenario)

        # end of solve loop

        self._extract_coordinate_derivatives(scenario,self.model.bodies,steps)

        return 0

    def _solve_unsteady_forward(self,scenario,steps=None):
        """
        This function solves the unsteady forward problem using NLBGS without FSI subiterations

        Parameters
        ----------
        scenario: :class:`~scenario.Scenario`
            the current scenario
        steps: int
            number of time steps if not using the value defined in the scenario

        Returns
        -------
        fail: int
            fail flag for the coupled solver

        """
        fail = 0

        if not steps:
            if not self.fakemodel:
                steps = scenario.steps
            else:
                if self.comm.Get_rank()==0:
                    print("No number of steps given for the coupled problem. Using default (1000)")
                steps = 1000

        for step in range(1,steps+1):
            # Transfer structural displacements to aerodynamic surface
            for body in self.model.bodies:

                body.aero_disps = np.zeros(body.aero_nnodes*3,dtype=TransferScheme.dtype)
                body.transfer.transferDisps(body.struct_disps, body.aero_disps)

                if ('rigid'  in body.motion_type and
                    'deform' in body.motion_type):
                    rotation = np.zeros(9,dtype=TransferScheme.dtype)
                    translation = np.zeros(3,dtype=TransferScheme.dtype)
                    u = np.zeros(body.aero_nnodes*3,dtype=TransferScheme.dtype)
                    body.rigid_transform = np.zeros((4,4),dtype=TransferScheme.dtype)

                    body.transfer.transformEquivRigidMotion(body.aero_disps, rotation, translation, u)

                    body.rigid_transform[:3,:3] = rotation.reshape((3,3,),order='F')
                    body.rigid_transform[:3, 3] = translation
                    body.rigid_transform[-1,-1] = 1.0

                    body.aero_disps = u.copy()

                elif('rigid' in body.motion_type):
                    transform = self.solvers['structural'].get_rigid_transform(body)

            fail = self.solvers['flow'].iterate(scenario,self.model.bodies,step)

            fail = self.comm.allreduce(fail)
            if fail != 0:
                if self.comm.Get_rank() == 0:
                    print('Flow solver returned fail flag')
                return fail

            # Transfer loads from fluid and get loads on structure
            for body in self.model.bodies:
                body.struct_loads = np.zeros(body.struct_nnodes*body.xfer_ndof, dtype=TransferScheme.dtype)
                body.transfer.transferLoads(body.aero_loads, body.struct_loads)

            # Take a step in the FEM model
            fail = self.solvers['structural'].iterate(scenario,self.model.bodies,step)

            fail = self.comm.allreduce(fail)
            if fail != 0:
                if self.comm.Get_rank() == 0:
                    print('Structural solver returned fail flag')
                return fail

        # end solve loop
        return fail

    def _solve_unsteady_adjoint(self,scenario):
        """
        Solves the unsteady adjoint problem using LBGS without FSI subiterations

        Parameters
        ----------
        scenario: :class:`~scenario.Scenario`
            the current scenario
        steps: int
            number of time steps

        Returns
        -------
        fail: int
            fail flag

        """
        # Initialize the adjoint variables
        nfunctions = scenario.count_adjoint_functions()
        self._initialize_adjoint_variables(scenario,self.model.bodies)

        steps = scenario.steps

        for rstep in range(1,steps+1):
            step = steps - rstep + 1

            self.solvers['flow'].set_states(scenario,self.model.bodies,step)
            # Due to the staggering, we linearize the transfer about u_s^(n-1)
            self.solvers['structural'].set_states(scenario,self.model.bodies,step-1)

            for body in self.model.bodies:
                body.aero_disps = np.zeros(body.aero_nnodes*3,dtype=TransferScheme.dtype)
                body.transfer.transferDisps(body.struct_disps,body.aero_disps)

                struct_loads = np.zeros(body.struct_nnodes*body.xfer_ndof,dtype=TransferScheme.dtype)
                body.transfer.transferLoads(body.aero_loads,struct_loads)

                if ('rigid'  in body.motion_type and
                    'deform' in body.motion_type):
                    rotation = np.zeros(9,dtype=TransferScheme.dtype)
                    translation = np.zeros(3,dtype=TransferScheme.dtype)
                    u = np.zeros(body.aero_nnodes*3,dtype=TransferScheme.dtype)

                    body.rigid_transform = np.zeros((4,4),dtype=TransferScheme.dtype)

                    body.transfer.transformEquivRigidMotion(body.aero_disps,rotation,translation,u)

                    body.rigid_transform[:3,:3] = rotation.reshape((3,3,),order='F')
                    body.rigid_transform[:3, 3] = translation
                    body.rigid_transform[-1,-1] = 1.0

                    body.global_aero_disps = body.aero_disps[:]
                    body.aero_disps = u.copy()

            # take a step in the structural adjoint
            fail = self.solvers['structural'].iterate_adjoint(scenario,self.model.bodies,step)

            fail = self.comm.allreduce(fail)
            if fail != 0:
                if self.comm.Get_rank() == 0:
                    print('Structural solver returned fail flag')
                return fail

            for body in self.model.bodies:
                for func in range(nfunctions):
                    # 'Solve' for load transfer adjoint variables
                    body.psi_L[:,func] = body.psi_S[:,func]

                    # Transform load transfer adjoint variables using transpose Jacobian from
                    # funtofem: dLdfA^T * psi_L
                    psi_L_r = np.zeros(body.aero_nnodes*3,dtype=TransferScheme.dtype)
                    body.transfer.applydDduS(body.psi_L[:, func].copy(order='C'), psi_L_r)
                    body.dLdfa[:,func] = psi_L_r

            fail = self.solvers['flow'].iterate_adjoint(scenario,self.model.bodies,step)

            fail = self.comm.allreduce(fail)
            if fail != 0:
                if self.comm.Get_rank() == 0:
                    print('Flow solver returned fail flag')
                return fail

            # From the flow grid adjoint, get to the displacement adjoint
            for body in self.model.bodies:
                for func in range(nfunctions):
                    if body.motion_type == 'deform':
                        # displacement adjoint equation
                        body.psi_D[:,func] = - body.dGdua[:,func]
                    elif 'rigid' in body.motion_type and 'deform' in body.motion_type:
                        # solve the elastic deformation adjoint
                        psi_E = np.zeros(body.aero_nnodes*3,dtype=TransferScheme.dtype)
                        tmt = np.linalg.inv(np.transpose(body.rigid_transform))
                        for node in range(body.aero_nnodes):
                            for i in range(3):
                                psi_E[3*node+i] = (  tmt[i,0] * body.dGdua[3*node+0,func]
                                                   + tmt[i,1] * body.dGdua[3*node+1,func]
                                                   + tmt[i,2] * body.dGdua[3*node+2,func]
                                                   + tmt[i,3]                            )

                        # get the product dE/dT^T psi_E
                        dEdTmat = np.zeros((3,4),dtype=TransferScheme.dtype)

                        for n in range(body.aero_nnodes):
                            for i in range(3):
                                for j in range(4):
                                    if j < 3:
                                        dEdTmat[i,j] += -(body.aero_X[3*n+j]+body.aero_disps[3*n+j]) * psi_E[3*n+i]
                                    else:
                                        dEdTmat[i,j] += - psi_E[3*n+i]

                        dEdT = dEdTmat.flatten(order='F')

                        dEdT = self.comm.allreduce(dEdT)

                        # solve the rigid transform adjoint
                        psi_R = np.zeros(12,dtype=TransferScheme.dtype)
                        dGdT_func = body.dGdT[:,:,func]
                        dGdT = dGdT_func[:3,:4].flatten(order='F')

                        psi_R = -dGdT - dEdT

                        # now solve the displacement adjoint
                        dRduA = np.zeros(3*body.aero_nnodes,dtype=TransferScheme.dtype)
                        body.transfer.applydRduATrans(psi_R, dRduA)

                        body.psi_D[:,func] = - psi_E - dRduA

                # form the RHS for the structural adjoint equation on the next reverse step
                for func in range(nfunctions):
                    # calculate dDdu_s^T * psi_D
                    psi_D_product = np.zeros(body.struct_nnodes*body.xfer_ndof,dtype=TransferScheme.dtype)
                    body.transfer.applydDduSTrans(body.psi_D[:,func].copy(order='C'), psi_D_product)

                    # calculate dLdu_s^T * psi_L
                    psi_L_product = np.zeros(body.struct_nnodes*body.xfer_ndof,dtype=TransferScheme.dtype)
                    body.transfer.applydLduSTrans(body.psi_L[:,func].copy(order='C'), psi_L_product)
                    body.struct_rhs[:,func] = -psi_D_product - psi_L_product

            # extract and accumulate coordinate derivative every step
            self._extract_coordinate_derivatives(scenario,self.model.bodies,step)

        # end of solve loop

        # evaluate the initial conditions
        fail = self.solvers['flow'].iterate_adjoint(scenario, self.model.bodies, step=0)
        fail = self.comm.allreduce(fail)
        if fail != 0:
            if self.comm.Get_rank() == 0:
                print('Flow solver returned fail flag')
            return fail
        fail = self.solvers['structural'].iterate_adjoint(scenario, self.model.bodies, step=0)
        fail = self.comm.allreduce(fail)
        if fail != 0:
            if self.comm.Get_rank() == 0:
                print('Structural solver returned fail flag')
            return fail

        # extract coordinate derivative term from initial condition
        self._extract_coordinate_derivatives(scenario,self.model.bodies,step=0)

        fail = 0
        return fail

    def _aitken_relax(self):
        if self.aitken_init:
            self.aitken_init = False

            # initialize the 'previous update' to zero
            self.up_prev = []
            self.aitken_vec = []
            self.theta = []

            for ind, body in enumerate(self.model.bodies):
                self.up_prev.append(np.zeros(body.struct_nnodes*body.xfer_ndof,dtype=TransferScheme.dtype))
                self.aitken_vec.append(np.zeros(body.struct_nnodes*body.xfer_ndof,dtype=TransferScheme.dtype))
                self.theta.append(self.theta_init)

        # do the Aitken update
        for ibody, body in enumerate(self.model.bodies):
            if body.struct_nnodes > 0:
                up = body.struct_disps - self.aitken_vec[ibody]
                norm2 = (np.linalg.norm(up - self.up_prev[ibody])**2.0)

                # Only update theta if the displacements changed
                if norm2 > 1e-13:
                    self.theta[ibody] *= 1.0 - (up - self.up_prev[ibody]).dot(up)/norm2
                    self.theta[ibody] = np.max((np.min((self.theta[ibody],self.theta_max)),self.theta_min))

                # handle the min/max for complex step
                if type(self.theta[ibody]) == np.complex128 or type(self.theta[ibody]) == complex:
                    self.theta[ibody] = self.theta[ibody].real + 0.0j

                self.aitken_vec[ibody] += self.theta[ibody] * up
                self.up_prev[ibody] = up[:]
                body.struct_disps = self.aitken_vec[ibody]

        return

    def _aitken_adjoint_relax(self,scenario):
        nfunctions =  scenario.count_adjoint_functions()
        if self.aitken_init:
            self.aitken_init = False

            # initialize the 'previous update' to zero
            self.up_prev = []
            self.aitken_vec = []
            self.theta = []

            for ibody, body in enumerate(self.model.bodies):
                up_prev_body = []
                aitken_vec_body = []
                theta_body = []
                for func in range(nfunctions):
                    up_prev_body.append(np.zeros(body.struct_nnodes*body.xfer_ndof,dtype=TransferScheme.dtype))
                    aitken_vec_body.append(np.zeros(body.struct_nnodes*body.xfer_ndof,dtype=TransferScheme.dtype))
                    theta_body.append(self.theta_init)
                self.up_prev.append(up_prev_body)
                self.aitken_vec.append(aitken_vec_body)
                self.theta.append(theta_body)

        # do the Aitken update
        for ibody, body in enumerate(self.model.bodies):
            if body.struct_nnodes > 0:
                for func in range(nfunctions):
                    up = body.psi_S[:,func] - self.aitken_vec[ibody][func]
                    norm2 = np.linalg.norm(up - self.up_prev[ibody][func])**2.0

                    # Only update theta if the vector changed
                    if norm2 > 1e-13:
                        self.theta[ibody][func] *= 1.0 - (up - self.up_prev[ibody][func]).dot(up)/np.linalg.norm(up - self.up_prev[ibody][func])**2.0
                        self.theta[ibody][func] = np.max((np.min((self.theta[ibody][func],self.theta_max)),self.theta_min))
                    self.aitken_vec[ibody][func] += self.theta[ibody][func] * up
                    self.up_prev[ibody][func] =up[:]
                    body.psi_S[:,func] = self.aitken_vec[ibody][func][:]

        return self.aitken_vec
