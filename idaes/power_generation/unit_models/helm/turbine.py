##############################################################################
# Institute for the Design of Advanced Energy Systems Process Systems
# Engineering Framework (IDAES PSE Framework) Copyright (c) 2018-2019, by the
# software owners: The Regents of the University of California, through
# Lawrence Berkeley National Laboratory,  National Technology & Engineering
# Solutions of Sandia, LLC, Carnegie Mellon University, West Virginia
# University Research Corporation, et al. All rights reserved.
#
# Please see the files COPYRIGHT.txt and LICENSE.txt for full copyright and
# license information, respectively. Both files are also available online
# at the URL "https://github.com/IDAES/idaes-pse".
##############################################################################
import pyomo.environ as pyo
from pyomo.common.config import In
from idaes.core import declare_process_block_class
from idaes.generic_models.unit_models.heater import HeaterData
from idaes.core.util import from_json, to_json, StoreSpec
import idaes.generic_models.properties.helmholtz.helmholtz as hltz
from idaes.generic_models.properties.helmholtz.helmholtz import (
    HelmholtzThermoExpressions as ThermoExpr
)
import idaes.logger as idaeslog

_log = idaeslog.getLogger(__name__)


def _assert_properties(pb):
    """Assert that the properies parameter block conforms to the requirements"""
    try:
        assert isinstance(pb, hltz.HelmholtzParameterBlockData)
        assert pb.config.phase_presentation in {
            hltz.PhaseType.MIX, hltz.PhaseType.L, hltz.PhaseType.G}
        assert pb.config.state_vars == hltz.StateVars.PH
    except AssertionError:
        _log.error("helm.IsentropicTurbine requires a Helmholtz EOS with "
                   "a single or mixed phase and pressure-enthalpy state vars.")
        raise


@declare_process_block_class("IsentropicTurbine")
class IsentropicTurbineData(HeaterData):
    """
    Basic isentropic 0D turbine model.  This inherits the heater block to get
    a lot of unit model boilerplate and the mass balance, enegy balance and
    pressure equations.  This model is intended to be used only with Helmholtz
    EOS property pacakges in mixed or single phase mode with P-H state vars.

    Since this inherits HeaterData, and only operates in steady-state or
    pseudo-steady-state (for dynamic models) the following mass, energy and
    pressure equations are implicitly writen.

    1) Mass Balance:
        0 = flow_mol_in[t] - flow_mol_out[t]
    2) Energy Balance:
        0 = (flow_mol[t]*h_mol[t])_in - (flow_mol[t]*h_mol[t])_out + Q_in + W_in
    3) Pressure:
        0 = P_in[t] + deltaP[t] - P_out[t]
    """

    CONFIG = HeaterData.CONFIG()
    # For dynamics assume pseudo-steady-state
    CONFIG.dynamic = False
    CONFIG.get("dynamic")._default = False
    CONFIG.get("dynamic")._domain = In([False])
    CONFIG.has_holdup = False
    CONFIG.get("has_holdup")._default = False
    CONFIG.get("has_holdup")._domain = In([False])
    # Rest of config to make this function like a turbine
    CONFIG.has_pressure_change = True
    CONFIG.get("has_pressure_change")._default = True
    CONFIG.get("has_pressure_change")._domain = In([True])
    CONFIG.has_work_transfer = True
    CONFIG.get("has_work_transfer")._default = True
    CONFIG.get("has_work_transfer")._domain = In([True])

    def build(self):
        """
        Add model equations to the unit model.  This is called by a default block
        construnction rule when the unit model is created.
        """
        super().build() # Basic unit model build/read config
        config = self.config # shorter config pointer

        # Thermo dynamic expression writer
        _assert_properties(config.property_package)
        te = ThermoExpr(blk=self, parameters=config.property_package)

        eff = self.efficiency_isentropic = pyo.Var(
            self.flowsheet().config.time,
            initialize=0.9,
            doc="Isentropic efficiency"
        )
        eff.fix()

        pratio = self.ratioP = pyo.Var(
            self.flowsheet().config.time,
            initialize=0.7,
            doc="Ratio of outlet to inlet pressure"
        )

        self.control_volume.heat.fix()

        # Some shorter refernces to property blocks
        prp_i = self.control_volume.properties_in
        prp_o = self.control_volume.properties_out

        @self.Expression(self.flowsheet().config.time)
        def h_is(b, t):
            return te.h(s=prp_i[t].entr_mol, p=prp_o[t].pressure)

        @self.Expression(self.flowsheet().config.time)
        def work_isentropic(b, t):
            return (prp_i[t].enth_mol - self.h_is[t])*prp_i[t].flow_mol

        @self.Expression(self.flowsheet().config.time)
        def h_o(b, t): # Early access to the outlet enthalpy and work
            return prp_i[t].enth_mol - eff[t]*(prp_i[t].enth_mol - self.h_is[t])

        @self.Constraint(self.flowsheet().config.time)
        def eq_work(b, t): # Work from energy balance
            return prp_o[t].enth_mol == self.h_o[t]

        @self.Constraint(self.flowsheet().config.time)
        def eq_pressure_ratio(b, t):
            return pratio[t]*prp_i[t].pressure == prp_o[t].pressure


    def initialize(
        self,
        outlvl=idaeslog.NOTSET,
        solver="ipopt",
        optarg={"tol": 1e-6},
    ):
        """
        For simplicity this initialization requires you to set values for the
        efficency, inlet, and one of pressure ratio, pressure change or outlet
        pressure.
        """
        init_log = idaeslog.getInitLogger(self.name, outlvl, tag="unit")
        solve_log = idaeslog.getSolveLogger(self.name, outlvl, tag="unit")
        # Set solver options
        solver = pyo.SolverFactory(solver)
        solver.options = optarg
        # Store original specification so initialization doesn't change the model
        # This will only resore the values of varaibles that were originally fixed
        sp = StoreSpec.value_isfixed_isactive(only_fixed=True)
        istate = to_json(self, return_dict=True, wts=sp)
        # Check for alternate pressure specs
        for t in self.flowsheet().config.time:
            if self.outlet.pressure[t].fixed:
                self.ratioP[t] = pyo.value(
                    self.outlet.pressure[t]/self.inlet.pressure[t])
            elif self.control_volume.deltaP[t].fixed:
                self.ratioP[t] = pyo.value(
                    (self.control_volume.deltaP[t] + self.inlet.pressure[t])/
                    self.inlet.pressure[t]
                )
        # Fix the variables we base the initializtion on and free the rest.
        # This requires good values to be provided for pressure, efficency,
        # and inlet conditions, but it is simple and reliable.
        self.inlet.fix()
        self.outlet.unfix()
        self.ratioP.fix()
        self.deltaP.unfix()
        self.efficiency_isentropic.fix()
        for t in self.flowsheet().config.time:
            self.outlet.pressure[t] = pyo.value(
                self.inlet.pressure[t]*self.ratioP[t])
            self.deltaP[t] = pyo.value(
                self.outlet.pressure[t] - self.inlet.pressure[t])
            self.outlet.enth_mol[t] = pyo.value(self.h_o[t])
            self.control_volume.work[t] = pyo.value(
                self.inlet.flow_mol[t]*self.inlet.enth_mol[t] -
                self.outlet.flow_mol[t]*self.outlet.enth_mol[t]
            )
            self.outlet.flow_mol[t] = pyo.value(self.inlet.flow_mol[t])
        # Solve the model (should be already solved from above)
        with idaeslog.solver_log(solve_log, idaeslog.DEBUG) as slc:
            res = solver.solve(self, tee=slc.tee)
        from_json(self, sd=istate, wts=sp)
