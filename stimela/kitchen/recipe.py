from multiprocessing import cpu_count
import os, os.path, re, logging, fnmatch, copy
from typing import Any, Tuple, List, Dict, Optional, Union
from dataclasses import dataclass
from omegaconf import MISSING, OmegaConf, DictConfig, ListConfig
from collections import OrderedDict
from collections.abc import Mapping
from pytest import param
from scabha import cargo
from pathos.pools import ProcessPool
from pathos.serial import SerialPool
from multiprocessing import cpu_count
from stimela.config import EmptyDictDefault, EmptyListDefault, StimelaLogConfig
import stimela
from stimela import logger, stimelogging
from stimela.exceptions import *
from scabha import validate
import scabha.exceptions
from scabha.exceptions import SubstitutionError, SubstitutionErrorList
from scabha.validate import Unresolved, join_quote
from scabha.substitutions import SubstitutionNS, substitutions_from 
from scabha.cargo import Parameter, Cargo, Cab, Batch
from scabha.types import File, Directory, MS


from . import runners

Conditional = Optional[str]

class DeferredAlias(Unresolved):
    """Class used as placeholder for deferred alias lookup (i.e. before an aliased value is available)"""
    pass

@dataclass
class Step:
    """Represents one processing step of a recipe"""
    cab: Optional[str] = None                       # if not None, this step is a cab and this is the cab name
    recipe: Optional["Recipe"] = None               # if not None, this step is a nested recipe
    params: Dict[str, Any] = EmptyDictDefault()     # assigns parameter values
    info: Optional[str] = None                      # comment or info
    skip: bool = False                              # if true, step is skipped unless explicitly enabled
    tags: List[str] = EmptyListDefault()
    backend: Optional[str] = None                   # backend setting, overrides opts.config.backend if set

    name: str = ''                                  # step's internal name
    fqname: str = ''                                # fully-qualified name e.g. recipe_name.step_label

    assign: Dict[str, Any] = EmptyDictDefault()     # assigns variables when step is executed

    assign_based_on: Dict[str, Any] = EmptyDictDefault()
                                                    # assigns variables when step is executed based on value of another variable

    _skip: Conditional = None                       # skip this step if conditional evaluates to true
    _break_on: Conditional = None                   # break out (of parent recipe) if conditional evaluates to true

    def __post_init__(self):
        self.fqname = self.fqname or self.name
        if bool(self.cab) == bool(self.recipe):
            raise StepValidationError("step must specify either a cab or a nested recipe, but not both")
        self.cargo = self.config = None
        self.tags = set(self.tags)
        # convert params into stadard dict, else lousy stuff happens when we insert non-standard objects
        if isinstance(self.params, DictConfig):
            self.params = OmegaConf.to_container(self.params)
        # after (pre)validation, this contains parameter values
        self.validated_params = None

    def summary(self, params=None, recursive=True, ignore_missing=False):
        return self.cargo and self.cargo.summary(recursive=recursive, 
                                params=params or self.validated_params or self.params, ignore_missing=ignore_missing)

    @property
    def finalized(self):
        return self.cargo is not None

    @property
    def missing_params(self):
        return OrderedDict([(name, schema) for name, schema in self.cargo.inputs_outputs.items() 
                            if schema.required and name not in self.validated_params])

    @property
    def invalid_params(self):
        return [name for name, value in self.validated_params.items() if isinstance(value, scabha.exceptions.Error)]

    @property
    def unresolved_params(self):
        return [name for name, value in self.validated_params.items() if isinstance(value, Unresolved)]

    @property
    def inputs(self):
        return self.cargo.inputs

    @property
    def outputs(self):
        return self.cargo.outputs

    @property
    def inputs_outputs(self):
        return self.cargo.inputs_outputs

    @property
    def log(self):
        """Logger object passed from cargo"""
        return self.cargo and self.cargo.log
    
    @property
    def logopts(self):
        """Logger options passed from cargo"""
        return self.cargo and self.cargo.logopts

    @property
    def nesting(self):
        """Logger object passed from cargo"""
        return self.cargo and self.cargo.nesting

    def update_parameter(self, name, value):
        self.params[name] = value

    def finalize(self, config=None, log=None, logopts=None, fqname=None, nesting=0):
        if not self.finalized:
            if fqname is not None:
                self.fqname = fqname
            self.config = config = config or stimela.CONFIG

            if bool(self.cab) == bool(self.recipe):
                raise StepValidationError("step must specify either a cab or a nested recipe, but not both")
            # if recipe, validate the recipe with our parameters
            if self.recipe:
                # instantiate from omegaconf object, if needed
                if type(self.recipe) is not Recipe:
                    self.recipe = Recipe(**self.recipe)
                self.cargo = self.recipe
            else:
                if self.cab not in self.config.cabs:
                    raise StepValidationError(f"unknown cab {self.cab}")
                self.cargo = Cab(**config.cabs[self.cab])
            self.cargo.name = self.name

            # if logger is not provided, then init one
            if log is None:
                log = stimela.logger().getChild(self.fqname)
                log.propagate = True

            # init and/or update logger options
            logopts = (logopts if logopts is not None else self.config.opts.log).copy()
            if 'log' in self.assign:
                logopts.update(**self.assign.log)

            # update file logging if not recipe (a recipe will do it in its finalize() anyway, with its own substitions)
            if not self.recipe:
                logsubst = SubstitutionNS(config=config, info=dict(fqname=self.fqname))
                stimelogging.update_file_logger(log, logopts, nesting=nesting, subst=logsubst, location=[self.fqname])

            # finalize the cargo
            self.cargo.finalize(config, log=log, logopts=logopts, fqname=self.fqname, nesting=nesting)

            # set backend
            self.backend = self.backend or self.config.opts.backend


    def prevalidate(self, subst: Optional[SubstitutionNS]=None):
        self.finalize()
        # validate cab or recipe
        params = self.validated_params = self.cargo.prevalidate(self.params, subst)
        self.log.debug(f"{self.cargo.name}: {len(self.missing_params)} missing, "
                        f"{len(self.invalid_params)} invalid and "
                        f"{len(self.unresolved_params)} unresolved parameters")
        if self.invalid_params:
            raise StepValidationError(f"{self.cargo.name} has the following invalid parameters: {join_quote(self.invalid_params)}")
        return params

    def log_summary(self, level, title, color=None, ignore_missing=False):
        extra = dict(color=color, boldface=True)
        if self.log.isEnabledFor(level):
            self.log.log(level, f"### {title}", extra=extra)
            del extra['boldface']
            for line in self.summary(recursive=False, ignore_missing=ignore_missing):
                self.log.log(level, line, extra=extra)

    def run(self, subst=None, batch=None):
        """Runs the step"""
        if self.validated_params is None:
            self.prevalidate(self.params)

        # Since prevalidation will have populated default values for potentially missing parameters, use those values
        # For parameters that aren't missing, use whatever value that was suplied
        params = self.validated_params.copy()
        params.update(**self.params)

        # # However the unresolved ones should be 
        # params = self.validated_params
        # for name, value in params.items():
        #     if type(value) is Unresolved:
        #         params[name] = self.params[name]
        # # params = self.params

        skip_warned = False   # becomes True when warnings are given

        self.log.debug(f"validating inputs {subst and list(subst.keys())}")
        validated = None
        try:
            params = self.cargo.validate_inputs(params, loosely=self.skip, subst=subst)
            validated = True

        except ScabhaBaseException as exc:
            level = logging.WARNING if self.skip else logging.ERROR
            if not exc.logged:
                if type(exc) is SubstitutionErrorList:
                    self.log.log(level, f"unresolved {{}}-substitution(s):")
                    for err in exc.errors:
                        self.log.log(level, f"  {err}")
                else:
                    self.log.log(level, f"error validating inputs: {exc}")
                exc.logged = True
            self.log_summary(level, "summary of inputs follows", color="WARNING")
            # raise up, unless step is being skipped
            if self.skip:
                self.log.warning("since the step is being skipped, this is not fatal")
                skip_warned = True
            else:
                raise

        self.validated_params.update(**params)

        # log inputs
        if validated and not self.skip:
            self.log_summary(logging.INFO, "validated inputs", color="GREEN", ignore_missing=True)
            if subst is not None:
                subst.current = params

        # bomb out if some inputs failed to validate or substitutions resolve
        if self.invalid_params or self.unresolved_params:
            invalid = self.invalid_params + self.unresolved_params
            if self.skip:
                self.log.warning(f"invalid inputs: {join_quote(invalid)}")
                if not skip_warned:
                    self.log.warning("since the step was skipped, this is not fatal")
                    skip_warned = True
            else:
                raise StepValidationError(f"invalid inputs: {join_quote(invalid)}", log=self.log)

        if not self.skip:
            try:
                if type(self.cargo) is Recipe:
                    self.cargo.backend = self.cargo.backend or self.backend
                    self.cargo._run(params)
                elif type(self.cargo) is Cab:
                    self.cargo.backend = self.cargo.backend or self.backend
                    runners.run_cab(self.cargo, params, log=self.log, subst=subst, batch=batch)
                else:
                    raise RuntimeError("Unknown cargo type")
            except ScabhaBaseException as exc:
                if not exc.logged:
                    self.log.error(f"error running step: {exc}")
                    exc.logged = True
                raise

        self.log.debug(f"validating outputs")
        validated = False

        try:
            params = self.cargo.validate_outputs(params, loosely=self.skip, subst=subst)
            validated = True
        except ScabhaBaseException as exc:
            level = logging.WARNING if self.skip else logging.ERROR
            if not exc.logged:
                if type(exc) is SubstitutionErrorList:
                    self.log.log(level, f"unresolved {{}}-substitution(s):")
                    for err in exc.errors:
                        self.log.log(level, f"  {err}")
                else:
                    self.log.log(level, f"error validating outputs: {exc}")
                exc.logged = True
            # raise up, unless step is being skipped
            if self.skip:
                self.log.warning("since the step was skipped, this is not fatal")
            else:
                self.log_summary(level, "failed outputs", color="WARNING")
                raise

        if validated:
            self.validated_params.update(**params)
            if subst is not None:
                subst.current._merge_(params)
            self.log_summary(logging.DEBUG, "validated outputs", ignore_missing=True)

        # bomb out if an output was invalid
        invalid = [name for name in self.invalid_params + self.unresolved_params if name in self.cargo.outputs]
        if invalid:
            if self.skip:
                self.log.warning(f"invalid outputs: {join_quote(invalid)}")
                self.log.warning("since the step was skipped, this is not fatal")
            else:
                raise StepValidationError(f"invalid outputs: {join_quote(invalid)}", log=self.log)

        return params

@dataclass
class ForLoopClause(object):
    # name of list variable
    var: str 
    # This should be the name of an input that provides a list, or a list
    over: Any
    # If True, this is a scatter not a loop -- things may be evaluated in parallel
    scatter: bool = False



@dataclass
class Recipe(Cargo):
    """Represents a sequence of steps.

    Additional attributes available after validation with arguments are as per for a Cab:

        self.input_output:      combined parameter dict (self.input + self.output), maps name to Parameter

    Raises:
        various classes of validation errors
    """
    steps: Dict[str, Step] = EmptyDictDefault()     # sequence of named steps

    assign: Dict[str, Any] = EmptyDictDefault()     # assigns variables

    assign_based_on: Dict[str, Any] = EmptyDictDefault()
                                                    # assigns variables based on values of other variables

    aliases: Dict[str, Any] = EmptyDictDefault()

    defaults: Dict[str, Any] = EmptyDictDefault()

    # make recipe a for_loop-gather (i.e. parallel for loop)
    for_loop: Optional[ForLoopClause] = None

    # logging control, overrides opts.log.init_logname and opts.log.logname 
    init_logname: Optional[str] = None
    logname: Optional[str] = None
    batch: Optional[Batch] = None
    
    # # if not None, do a while loop with the conditional
    # _while: Conditional = None
    # # if not None, do an until loop with the conditional
    # _until: Conditional = None

    def __post_init__ (self):
        Cargo.__post_init__(self)
        # check that schemas are valid
        for io in self.inputs, self.outputs:
            for name, schema in io.items():
                if not schema:
                    raise RecipeValidationError(f"'{name}' does not define a valid schema")
        # check for repeated aliases
        for name, alias_list in self.aliases.items():
            if name in self.inputs_outputs:
                raise RecipeValidationError(f"alias '{name}' also appears under inputs or outputs")
            if type(alias_list) is str:
                alias_list = self.aliases[name] = [alias_list]
            if not hasattr(alias_list, '__iter__') or not all(type(x) is str for x in alias_list):
                raise RecipeValidationError(f"alias '{name}': name or list of names expected")
            for x in alias_list:
                if '.' not in x:
                    raise RecipeValidationError(f"alias '{name}': invalid target '{x}' (missing dot)")
        # instantiate steps if needed (when creating from an omegaconf)
        if type(self.steps) is not OrderedDict:
            steps = OrderedDict()
            for label, stepconfig in self.steps.items():
                stepconfig.name = label
                stepconfig.fqname = f"{self.name}.{label}"
                steps[label] = Step(**stepconfig)
            self.steps = steps
        # check that assignments don't clash with i/o parameters
        self.validate_assignments(self.assign, self.assign_based_on, self.name)

        # check that for-loop variable does not clash
        if self.for_loop:
            for io, io_label in [(self.inputs, "inputs"), (self.outputs, "outputs")]:
                if self.for_loop.var in io:
                    raise RecipeValidationError(f"'for_loop.var={self.for_loop.var}' clashes with recipe {io_label}")
        # marked when finalized
        self._alias_map  = None
        # set of keys protected from assignment
        self._protected_from_assign = set()
        self._for_loop_values = None

    def protect_from_assignments(self, keys):
        self._protected_from_assign.update(keys)
        #self.log.debug(f"protected from assignment: {self._protected_from_assign}")

    def validate_assignments(self, assign, assign_based_on, location):
        # collect a list of all assignments
        assignments = OrderedDict()
        for key in assign:
            assignments[key] = "assign"
        for basevar, lookup_list in assign_based_on.items():
            if not isinstance(lookup_list, Mapping):
                raise RecipeValidationError(f"{location}.{assign_based_on}.{basevar}: mapping expected")
            # for assign_list in lookup_list.values():
            #     for key in assign_list:
            #         assignments[key] = f"assign_based_on.{basevar}"
        # # check that none clash
        # for key, assign_label in assignments.items():
        #     for io, io_label in [(self.inputs, "input"), (self.outputs, "output")]:
        #         if key in io:
        #             raise RecipeValidationError(f"'{location}.{assign_label}.{key}' clashes with an {io_label}")

    def update_assignments(self, assign, assign_based_on, params, location=""):
        for basevar, value_list in assign_based_on.items():
            # make sure the base variable is defined
            if basevar in assign:
                value = assign[basevar]
            elif basevar in params:
                value = params[basevar]
            elif basevar in self.inputs_outputs and self.inputs_outputs[basevar].default is not None:
                value = self.inputs_outputs[basevar].default
            else:
                raise AssignmentError(f"{location}.assign_based_on.{basevar} is an unset variable or parameter")
            # look up list of assignments
            assignments = value_list.get(value, value_list.get('DEFAULT'))
            if assignments is None:
                raise AssignmentError(f"{location}.assign_based_on.{basevar}: unknown value '{value}', and no default defined")
            # update assignments
            for key, value in assignments.items():
                if key in self._protected_from_assign:
                    self.log.debug(f"skipping protected assignment {key}={value}")
                else:
                    # vars with dots are config settings
                    if '.' in key:
                        self.log.debug(f"config assignment: {key}={value}")
                        path = key.split('.')
                        varname = path[-1]
                        section = self.config
                        for element in path[:-1]:
                            if element in section:
                                section = section[element]
                            else:
                                raise AssignmentError("{location}.assign_based_on.{basevar}: '{element}' in '{key}' is not a valid config section")
                        section[varname] = value
                    # vars without dots are local variables or parameters
                    else:
                        if key in self.inputs_outputs:
                            self.log.debug(f"params assignment: {key}={value}")
                            params[key] = value
                        else:
                            self.log.debug(f"variable assignment: {key}={value}")
                            self.assign[key] = value

    @property
    def finalized(self):
        return self._alias_map is not None

    def enable_step(self, label, enable=True):
        self.finalize()
        step = self.steps.get(label)
        if step is None:
            raise RecipeValidationError(f"unknown step {label}", log=self.log)
        if step.skip and enable:
            self.log.warning(f"enabling step '{label}' which was previously marked as skipped")
        elif not step.skip and not enable:
            self.log.warning(f"will skip step '{label}'")
        step.skip = not enable

    def restrict_steps(self, steps: List[str], force_enable=True):
        self.finalize()
        # check for unknown steps
        restrict_steps = set(steps)
        unknown_steps = restrict_steps.difference(self.steps)
        if unknown_steps:
            raise RecipeValidationError(f"unknown step(s) {join_quote(unknown_steps)}", log=self.log)

        # apply skip flags 
        for label, step in self.steps.items():
            if label not in restrict_steps:
                step.skip = True
            elif force_enable:
                step.skip = False

    def add_step(self, step: Step, label: str = None):
        """Adds a step to the recipe. Label is auto-generated if not supplied

        Args:
            step (Step): step object to add
            label (str, optional): step label, auto-generated if None
        """
        if self.finalized:
            raise DefinitionError("can't add a step to a recipe that's been finalized")

        names = [s for s in self.steps if s.cab == step.cabname]
        label = label or f"{step.cabname}_{len(names)+1}"
        self.steps[label] = step
        step.fqname = f"{self.name}.{label}"


    def add(self, cabname: str, label: str = None, 
            params: Optional[Dict[str, Any]] = None, info: str = None):
        """Add a step to a recipe. This will create a Step instance and call add_step() 

        Args:
            cabname (str): name of cab to use for this step
            label (str): Alphanumeric label (must start with a lette) for the step. If not given will be auto generated 'cabname_d' where d is the number of times a particular cab has been added to the recipe.
            params (Dict): A parameter dictionary
            info (str): Documentation of this step
        """
        return self.add_step(Step(cab=cabname, params=params, info=info), label=label)

    @dataclass
    class AliasInfo(object):
        label: str                      # step label
        step: Step                      # step
        param: str                      # parameter name
        io: Dict[str, Parameter]        # points to self.inputs or self.outputs
        from_recipe: bool = False       # if True, value propagates from recipe up to step
        from_step: bool = False         # if True, value propagates from step down to recipe

    def _add_alias(self, alias_name: str, alias_target: Union[str, Tuple]):
        if type(alias_target) is str:
            step_spec, step_param_name = alias_target.split('.', 1)

            # treat label as a "(cabtype)" specifier?
            if re.match('^\(.+\)$', step_spec):
                steps = [(label, step) for label, step in self.steps.items() if isinstance(step.cargo, Cab) and step.cab == step_spec[1:-1]]
            # treat label as a wildcard?
            elif any(ch in step_spec for ch in '*?['):
                steps = [(label, step) for label, step in self.steps.items() if fnmatch.fnmatchcase(label, step_spec)]
            # else treat label as a specific step name
            else:
                steps = [(step_spec, self.steps.get(step_spec))]
        else:
            step, step_spec, step_param_name = alias_target
            steps = [(step_spec, step)]

        for (step_label, step) in steps:
            if step is None:
                raise RecipeValidationError(f"alias '{alias_name}' refers to unknown step '{step_label}'", log=self.log)
            # is the alias already defined
            existing_alias = self._alias_list.get(alias_name, [None])[0]
            # find it in inputs or outputs
            input_schema = step.inputs.get(step_param_name)
            output_schema = step.outputs.get(step_param_name)
            schema = input_schema or output_schema
            if schema is None:
                raise RecipeValidationError(f"alias '{alias_name}' refers to unknown step parameter '{step_label}.{step_param_name}'", log=self.log)
            # implicit inuts cannot be aliased
            if input_schema and input_schema.implicit:
                raise RecipeValidationError(f"alias '{alias_name}' refers to implicit input '{step_label}.{step_param_name}'", log=self.log)
            # if alias is already defined, check for conflicts
            if existing_alias is not None:
                io = existing_alias.io
                if io is self.outputs:
                    raise RecipeValidationError(f"output alias '{alias_name}' is defined more than once", log=self.log)
                elif output_schema:
                    raise RecipeValidationError(f"alias '{alias_name}' refers to both an input and an output", log=self.log)
                alias_schema = io[alias_name] 
                # now we know it's a multiply-defined input, check for type consistency
                if alias_schema.dtype != schema.dtype:
                    raise RecipeValidationError(f"alias '{alias_name}': dtype {schema.dtype} of '{step_label}.{step_param_name}' doesn't match previous dtype {existing_schema.dtype}", log=self.log)
                
            # else alias not yet defined, insert a schema
            else:
                io = self.inputs if input_schema else self.outputs
                io[alias_name] = copy.copy(schema)
                alias_schema = io[alias_name]      # make copy of schema object

            # if step parameter is implicit, mark the alias as implicit. Note that this only applies to outputs
            if schema.implicit:
                alias_schema.implicit = Unresolved(f"{step_label}.{step_param_name}")   # will be resolved when propagated from step
                self._implicit_params.add(alias_name)

            # this is True if the step's parameter is defined in any way (set, default, or implicit)
            have_step_param = step_param_name in step.params or step_param_name in step.cargo.defaults or \
                schema.default is not None or schema.implicit is not None

            # if the step parameter is set, mark our schema as having a default
            if have_step_param:
                alias_schema.default = DeferredAlias(f"{step_label}.{step_param_name}")

            # alias becomes required if any step parameter it refers to was required, but wasn't already set 
            if schema.required and not have_step_param:
                alias_schema.required = True

            self._alias_map[step_label, step_param_name] = alias_name
            self._alias_list.setdefault(alias_name, []).append(Recipe.AliasInfo(step_label, step, step_param_name, io))

    def finalize(self, config=None, log=None, logopts=None, fqname=None, nesting=0):
        if not self.finalized:
            config = config or stimela.CONFIG

            # fully qualified name, i.e. recipe_name.step_name.step_name etc.
            self.fqname = fqname = fqname or self.fqname or self.name

            # if logger is not provided, then init one
            if log is None:
                log = stimela.logger().getChild(self.fqname)
                log.propagate = True

            # check that per-step assignments don't clash with i/o parameters
            for label, step in self.steps.items():
                self.validate_assignments(step.assign, step.assign_based_on, f"{fqname}.{label}")

            # init and/or update logger options
            logopts = (logopts if logopts is not None else config.opts.log).copy()
            if 'log' in self.assign:
                logopts.update(**self.assign.log)

            # update file logger
            logsubst = SubstitutionNS(config=config, info=dict(fqname=fqname))
            stimelogging.update_file_logger(log, logopts, nesting=nesting, subst=logsubst, location=[self.fqname])

            # call Cargo's finalize method
            super().finalize(config, log=log, logopts=logopts, fqname=fqname, nesting=nesting)

            # finalize steps
            for label, step in self.steps.items():
                step_log = log.getChild(label)
                step_log.propagate = True
                step.finalize(config, log=step_log, logopts=logopts, fqname=f"{fqname}.{label}", nesting=nesting+1)

            # collect aliases
            self._alias_map = OrderedDict()
            self._alias_list = OrderedDict()

            # collect from inputs and outputs
            for io in self.inputs, self.outputs:
                for name, schema in io.items():
                    if schema.aliases:
                        if schema.dtype != "str" or schema.choices or schema.writable:
                            raise RecipeValidationError(f"alias '{name}' should not specify type, choices or writability", log=log)
                        for alias_target in schema.aliases:
                            self._add_alias(name, alias_target)

            # collect from aliases section
            for name, alias_list in self.aliases.items():
                for alias_target in alias_list:
                    self._add_alias(name, alias_target)

            # automatically make aliases for step parameters that are unset, and don't have a default, and aren't implict 
            for label, step in self.steps.items():
                for name, schema in step.inputs_outputs.items():
                    if (label, name) not in self._alias_map and name not in step.params  \
                            and name not in step.cargo.defaults and schema.default is None \
                            and not schema.implicit:
                        auto_name = f"{label}_{name}"
                        if auto_name in self.inputs or auto_name in self.outputs:
                            raise RecipeValidationError(f"auto-generated parameter name '{auto_name}' conflicts with another name. Please define an explicit alias for this.", log=log)
                        self._add_alias(auto_name, (step, label, name))

            # these will be re-merged when needed again
            self._inputs_outputs = None

            # check that for-loop is valid, if defined
            if self.for_loop is not None:
                # if for_loop.over is a str, treat it as a required input
                if type(self.for_loop.over) is str:
                    if self.for_loop.over not in self.inputs:
                        raise RecipeValidationError(f"for_loop: over: '{self.for_loop.over}' is not a defined input", log=log)
                    # this becomes a required input
                    self.inputs[self.for_loop.over].required = True
                # else treat it as a list of values to be iterated over (and set over=None to indicate this)
                elif type(self.for_loop.over) in (list, tuple, ListConfig):
                    self._for_loop_values = list(self.for_loop.over)
                    self.for_loop.over = None
                else:
                    raise RecipeValidationError(f"for_loop: over is of invalid type {type(self.for_loop.over)}", log=log)

                # # insert empty loop variable
                # if self.for_loop.var not in self.assign:
                #     self.assign[self.for_loop.var] = ""

    def _prep_step(self, label, step, subst):
        parts = label.split("-")
        info = subst.info
        info.fqname = f"{self.fqname}.{label}"
        info.label = label 
        info.label_parts = parts
        info.suffix = parts[-1] if len(parts) > 1 else ''
        subst.current = step.params
        subst.steps[label] = subst.current

    def prevalidate(self, params: Optional[Dict[str, Any]], subst: Optional[SubstitutionNS]=None):
        self.finalize()
        self.log.debug("prevalidating recipe")
        errors = []

        # update assignments
        self.update_assignments(self.assign, self.assign_based_on, params=params, location=self.fqname)

        subst_outer = subst  # outer dictionary is used to prevalidate our parameters

        subst = SubstitutionNS()
        info = SubstitutionNS(fqname=self.fqname)
        # mutable=False means these sub-namespaces are not subject to {}-substitutions
        subst._add_('info', info, nosubst=True)
        subst._add_('config', self.config, nosubst=True) 
        subst._add_('steps', {}, nosubst=True)
        subst._add_('previous', {}, nosubst=True)
        subst._add_('recipe', self.make_substitition_namespace(params=params, ns=self.assign))
        subst.recipe._merge_(params)

        # add for-loop variable to inputs, if expected there
        if self.for_loop is not None and self.for_loop.var in self.inputs:
            params[self.for_loop.var] = Unresolved("for-loop")

        # prevalidate our own parameters. This substitutes in defaults and does {}-substitutions
        # we call this twice, potentially, so define as a function
        def prevalidate_self(params):
            try:
                params = Cargo.prevalidate(self, params, subst=subst_outer)
                # validate for-loop, if needed
                self.validate_for_loop(params, strict=False)

            except ScabhaBaseException as exc:
                msg = f"recipe pre-validation failed: {exc}"
                errors.append(RecipeValidationError(msg, log=self.log))

            # merge again, since values may have changed
            subst.recipe._merge_(params)
            return params

        params = prevalidate_self(params)

        # propagate alias values up to substeps, except for implicit values (these only ever propagate down to us)
        for name, aliases in self._alias_list.items():
            if name in params and type(params[name]) is not DeferredAlias and name not in self._implicit_params:
                for alias in aliases:
                    alias.from_recipe = True
                    alias.step.update_parameter(alias.param, params[name])

        # prevalidate step parameters 
        # we call this twice, potentially, so define as a function

        def prevalidate_steps():
            for label, step in self.steps.items():
                self._prep_step(label, step, subst)

                try:
                    step_params = step.prevalidate(subst)
                    subst.current._merge_(step_params)   # these may have changed in prevalidation
                except ScabhaBaseException as exc:
                    if type(exc) is SubstitutionErrorList:
                        self.log.error(f"unresolved {{}}-substitution(s):")
                        for err in exc.errors:
                            self.log.error(f"  {err}")
                    msg = f"step '{label}' failed pre-validation: {exc}"
                    errors.append(RecipeValidationError(msg, log=self.log))

                subst.previous = subst.current
                subst.steps[label] = subst.previous

        prevalidate_steps()

        # now check for aliases that need to be propagated up/down
        if not errors:
            revalidate_self = revalidate_steps = False
            for name, aliases in self._alias_list.items():
                # propagate up if alias is not set, or it is implicit=Unresolved (meaning it gets set from an implicit substep parameter)
                if name not in params or type(params[name]) is DeferredAlias or type(self.inputs_outputs[name].implicit) is Unresolved:
                    from_step = False
                    for alias in aliases:
                        # if alias is set in step but not with us, mark it as propagating down
                        if alias.param in alias.step.validated_params:
                            alias.from_step = from_step = revalidate_self = True
                            params[name] = alias.step.validated_params[alias.param]
                            # and break out, we do this for the first matching step only
                            break
                    # if we propagated an input value down from a step, check if we need to propagate it up to any other steps
                    # note that this only ever applies to inputs
                    if from_step:
                        for alias in aliases:
                            if not alias.from_step:
                                alias.from_recipe = revalidate_steps = True
                                alias.step.update_parameter(alias.param, params[name])

            # do we or any steps need to be revalidated?
            if revalidate_self:
                params = prevalidate_self(params)
            if revalidate_steps:
                prevalidate_steps()

        # check for missing parameters
        missing_params = [name for name, schema in self.inputs_outputs.items() if schema.required and name not in params]
        if missing_params:
            msg = f"""recipe '{self.name}' is missing the following required parameters: {join_quote(missing_params)}"""
            errors.append(RecipeValidationError(msg, log=self.log))

        if errors:
            raise RecipeValidationError(f"{len(errors)} error(s) validating the recipe '{self.name}'", log=self.log)

        self.log.debug("recipe pre-validated")

        return params

    def validate_for_loop(self, params, strict=False):
        # in case of for loops, get list of values to be iterated over 
        if self.for_loop is not None:
            # if over != None (see finalize() above), list of values needs to be looked `up in inputs
            # if it is None, then an explicit list was supplied and is already in self._for_loop_values.
            if self.for_loop.over is not None:
                # check that it's legal
                if self.for_loop.over in self.assign:
                    values = self.assign[self.for_loop.over]
                elif self.for_loop.over in params:
                    values = params[self.for_loop.over]
                elif self.for_loop.over not in self.inputs:
                    raise ParameterValidationError(f"for_loop.over={self.for_loop.over} does not refer to a known parameter")
                else:
                    raise ParameterValidationError(f"for_loop.over={self.for_loop.over} is unset")
                if strict and isinstance(values, Unresolved):
                    raise ParameterValidationError(f"for_loop.over={self.for_loop.over} is unresolved")
                if not isinstance(values, (list, tuple)):
                    values = [values]
                if self._for_loop_values is None:
                    self.log.info(f"recipe is a for-loop with '{self.for_loop.var}' iterating over {len(values)} values")
                    self.log.info(f"Loop values: {values}")
                self._for_loop_values = values
            if self.for_loop.var in self.inputs:
                params[self.for_loop.var] = self._for_loop_values[0]
            else:
                self.assign[self.for_loop.var] = self._for_loop_values[0]
        # else fake a single-value list
        else:
            self._for_loop_values = [None]

    def validate_inputs(self, params: Dict[str, Any], subst: Optional[SubstitutionNS]=None, loosely=False):

        self.validate_for_loop(params, strict=True)

        if subst is None:
            subst = SubstitutionNS()
            info = SubstitutionNS(fqname=self.fqname)
            subst._add_('info', info, nosubst=True)
            subst._add_('config', self.config, nosubst=True) 
            subst._add_('recipe', self.make_substitition_namespace(params=params, ns=self.assign))

        return Cargo.validate_inputs(self, params, subst=subst, loosely=loosely)

    def _link_steps(self):
        """
        Adds  next_step and previous_step attributes to the recipe. 
        """
        steps = list(self.steps.values())
        N = len(steps)
        # Nothing to link if only one step
        if N == 1:
            return

        for i in range(N):
            step = steps[i]
            if i == 0:
                step.next_step = steps[1]
                step.previous_step = None
            elif i > 0 and i < N-2:
                step.next_step = steps[i+1]
                step.previous_step = steps[i-1]
            elif i == N-1:
                step.next_step = None
                step.previous_step = steps[i-2]

    def summary(self, params: Dict[str, Any], recursive=True, ignore_missing=False):
        """Returns list of lines with a summary of the recipe state
        """
        lines = [f"recipe '{self.name}':"] + [f"  {name} = {value}" for name, value in params.items()]
        if not ignore_missing:
            lines += [f"  {name} = ???" for name in self.inputs_outputs if name not in params]
        if recursive:
            lines.append("  steps:")
            for name, step in self.steps.items():
                stepsum = step.summary()
                lines.append(f"    {name}: {stepsum[0]}")
                lines += [f"    {x}" for x in stepsum[1:]]
        return lines

    _root_recipe_ns = None

    def _run(self, params) -> Dict[str, Any]:
        """Internal recipe run method. Meant to be called from a wrapper Step object (which validates the parameters, etc.)

        Parameters
        ----------

        Returns
        -------
        Dict[str, Any]
            Dictionary of formal outputs

        Raises
        ------
        RecipeValidationError
        """

        # set up substitution namespace
        subst = SubstitutionNS()
        info = SubstitutionNS(fqname=self.fqname)
        # nosubst=True means these sub-namespaces are not subject to {}-substitutions
        subst._add_('info', info, nosubst=True)
        subst._add_('steps', {}, nosubst=True)
        subst._add_('previous', {}, nosubst=True)
        recipe_ns = self.make_substitition_namespace(params=params, ns=self.assign)
        subst._add_('recipe', recipe_ns)

        # merge in config sections, except "recipe" which clashes with our namespace
        for section, content in self.config.items():
            if section != 'recipe':
                subst._add_(section, content, nosubst=True)

        # add root-level recipe info
        if self.nesting <= 1:
            Recipe._root_recipe_ns = recipe_ns
        subst._add_('root', Recipe._root_recipe_ns)


        logopts = self.config.opts.log.copy()
        if 'log' in self.assign:
            logopts.update(**self.assign.log)
        
        # update logfile name (since this may depend on substitutions)
        stimelogging.update_file_logger(self.log, self.logopts, nesting=self.nesting, subst=subst, location=[self.fqname])

        # Harmonise before running
        self._link_steps()

        self.log.info(f"running recipe '{self.name}'")

        # our inputs have been validated, so propagate aliases to steps. Check for missing stuff just in case
        for name, schema in self.inputs.items():
            if name in params:
                value = params[name]
                if isinstance(value, Unresolved):
                    raise RecipeValidationError(f"recipe '{self.name}' has unresolved input '{name}'", log=self.log)
                # propagate up all aliases
                for alias in self._alias_list.get(name, []):
                    if alias.from_recipe:
                        alias.step.update_parameter(alias.param, value)
            else:
                if schema.required: 
                    raise RecipeValidationError(f"recipe '{self.name}' is missing required input '{name}'", log=self.log)



        # iterate over for-loop values (if not looping, this is set up to [None] in advance)
        scatter = getattr(self.for_loop, "scatter", False)
        
        def loop_worker(inst, step, label, subst, count, iter_var):
            """"
            Needed for concurrency
            """

            # if for-loop, assign new value
            if inst.for_loop:
                inst.log.info(f"for loop iteration {count}: {inst.for_loop.var} = {iter_var}")
                # update variables
                inst.assign[inst.for_loop.var] = iter_var
                inst.assign[f"{inst.for_loop.var}@index"] = count
                inst.update_assignments(inst.assign, inst.assign_based_on, inst.fqname)
                subst.recipe._merge_(inst.assign)
                # update logfile name (since this may depend on substitutions)
                stimelogging.update_file_logger(inst.log, inst.logopts, nesting=inst.nesting, subst=subst, location=[inst.fqname])


            # merge in variable assignments and add step params as "current" namespace
            self.update_assignments(step.assign, step.assign_based_on, f"{self.name}.{label}")
            subst.recipe._merge_(step.assign)
            
            # update info
            inst._prep_step(label, step, subst)
            # update log options again (based on assign.log which may have changed)
            if 'log' in step.assign:
                 logopts.update(**step.assign.log)

            # update logfile name regardless (since this may depend on substitutions)
            info.fqname = step.fqname
            stimelogging.update_file_logger(step.log, step.logopts, nesting=step.nesting, subst=subst, location=[step.fqname])

            inst.log.info(f"{'skipping' if step.skip else 'running'} step '{label}'")
            try:
                #step_params = step.run(subst=subst.copy(), batch=batch)  # make a copy of the subst dict since recipe might modify
                step_params = step.run(subst=subst.copy())  # make a copy of the subst dict since recipe might modify
            except ScabhaBaseException as exc:
                if not exc.logged:
                    inst.log.error(f"error running step '{label}': {exc}")
                    exc.logged = True
                raise

            # put step parameters into previous and steps[label] again, as they may have changed based on outputs)
            subst.previous = step_params
            subst.steps[label] = subst.previous

        loop_futures = []

        for count, iter_var in enumerate(self._for_loop_values):
            for label, step in self.steps.items():
                this_args = (self,step, label, subst, count, iter_var)
                loop_futures.append(this_args)

        # Transpose the list before parsing to pool.map()
        loop_args = list(map(list, zip(*loop_futures)))
        max_workers = getattr(self.config.opts.dist, "ncpu", cpu_count()//4)
        if scatter:
            loop_pool = ProcessPool(max_workers, scatter=True)
            results = loop_pool.amap(loop_worker, *loop_args)
            while not results.ready():
                time.sleep(1)
            results.get()
        else:
            # loop_pool = SerialPool(max_workers)
            # results = list(loop_pool.imap(loop_worker, *loop_args))
            results = [loop_worker(*args) for args in loop_futures]

        # now check for output aliases that need to be propagated down
        for name, aliases in self._alias_list.items():
            for alias in aliases:
                if alias.from_step:
                    if alias.param in alias.step.validated_params:
                        params[name] = alias.step.validated_params[alias.param]

        self.log.info(f"recipe '{self.name}' executed successfully")
        return OrderedDict((name, value) for name, value in params.items() if name in self.outputs)


    # def run(self, **params) -> Dict[str, Any]:
    #     """Public interface for running a step. Keywords are passed in as step parameters

    #     Returns
    #     -------
    #     Dict[str, Any]
    #         Dictionary of formal outputs
    #     """
    #     return Step(recipe=self, params=params, info=f"wrapper step for recipe '{self.name}'").run()


class PyRecipe(Recipe):
    """ 
        Interface to Recipe class for python recipes (not YAML recipes)
    """
    def __init__(self, name, dirs, backend=None, info=None, log=None):

        self.backend = backend
        self.name = name

        self.inputs: Dict[str, Any] = {}

        for dir_item in dirs:
            self.inputs[dir_item] = { 
                "dtype": Directory,
                "default": dirs[dir_item]
            }

