# Licensed under LICENSE.md; also available at https://www.prefect.io/licenses/alpha-eula

import collections
import datetime
import functools
import logging
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterable, List, Union, Set, Optional

import prefect
from prefect.core import Edge, Task
from prefect.engine import signals
from prefect.engine.state import (
    CachedState,
    Failed,
    Mapped,
    Pending,
    Retrying,
    Running,
    Skipped,
    State,
    Success,
    TriggerFailed,
)
from prefect.engine.runner import ENDRUN, Runner, call_state_handlers
from prefect.utilities.executors import main_thread_timeout


class TaskRunner(Runner):
    """
    TaskRunners handle the execution of Tasks and determine the State of a Task
    before, during and after the Task is run.

    In particular, through the TaskRunner you can specify the states of any upstream dependencies,
    any inputs required for this Task to run, and what state the Task should be initialized with.

    Args:
        - task (Task): the Task to be run / executed
        - state_handlers (Iterable[Callable], optional): A list of state change handlers
            that will be called whenever the task changes state, providing an
            opportunity to inspect or modify the new state. The handler
            will be passed the task runner instance, the old (prior) state, and the new
            (current) state, with the following signature:

            ```
                state_handler(
                    task_runner: TaskRunner,
                    old_state: State,
                    new_state: State) -> State
            ```

            If multiple functions are passed, then the `new_state` argument will be the
            result of the previous handler.
    """

    def __init__(self, task: Task, state_handlers: Iterable[Callable] = None) -> None:
        self.task = task
        super().__init__(state_handlers=state_handlers)

    def call_runner_target_handlers(self, old_state: State, new_state: State) -> State:
        """
        A special state handler that the TaskRunner uses to call its task's state handlers.
        This method is called as part of the base Runner's `handle_state_change()` method.

        Args:
            - old_state (State): the old (previous) state
            - new_state (State): the new (current) state

        Returns:
            State: the new state
        """
        for handler in self.task.state_handlers:
            new_state = handler(self.task, old_state, new_state)
        return new_state

    def run(
        self,
        state: State = None,
        upstream_states: Dict[Edge, Union[State, List[State]]] = None,
        inputs: Dict[str, Any] = None,
        ignore_trigger: bool = False,
        context: Dict[str, Any] = None,
        queues: Iterable = None,
        timeout_handler: Callable = None,
        mapped: bool = False,
    ) -> State:
        """
        The main endpoint for TaskRunners.  Calling this method will conditionally execute
        `self.task.run` with any provided inputs, assuming the upstream dependencies are in a
        state which allow this Task to run.

        Args:
            - state (State, optional): initial `State` to begin task run from;
                defaults to `Pending()`
            - upstream_states (Dict[Edge, Union[State, List[State]]]): a dictionary
                representing the states of any tasks upstream of this one. The keys of the
                dictionary should correspond to the edges leading to the task.
            - inputs (Dict[str, Any], optional): a dictionary of inputs whose keys correspond
                to the task's `run()` arguments. Any keys that are provided will override the
                `State`-based inputs provided in upstream_states.
            - ignore_trigger (bool): boolean specifying whether to ignore the
                Task trigger; defaults to `False`
            - context (dict, optional): prefect Context to use for execution
            - queues ([queue], optional): list of queues of tickets to use when deciding
                whether it's safe for the Task to run based on resource limitations. The
                Task will only begin running when a ticket from each queue is available.
            - timeout_handler (Callable, optional): function for timing out
                task execution, with call signature `handler(fn, *args, **kwargs)`. Defaults to
                `prefect.utilities.executors.main_thread_timeout`
            - mapped (bool, optional): whether this task is mapped; if `True`,
                the task will _not_ be run, but a `Mapped` state will be returned indicating
                it is ready to. Defaults to `False`

        Returns:
            - `State` object representing the final post-run state of the Task
        """

        queues = queues or []
        state = state or Pending()
        upstream_states = upstream_states or {}
        inputs = inputs or {}
        context = context or {}

        # construct task inputs
        task_inputs = {}
        for edge, v in upstream_states.items():
            if edge.key is None:
                continue
            if isinstance(v, list):
                task_inputs[edge.key] = [s.result for s in v]
            else:
                task_inputs[edge.key] = v.result
        task_inputs.update(inputs)

        # gather upstream states
        upstream_states_set = set(
            prefect.utilities.collections.flatten_seq(upstream_states.values())
        )

        # apply throttling
        while True:
            tickets = []
            for q in queues:
                try:
                    tickets.append(q.get(timeout=2))  # timeout after 2 seconds
                except Exception:
                    for ticket, q in zip(tickets, queues):
                        q.put(ticket)
            if len(tickets) == len(queues):
                break

        # run state transformation pipeline
        with prefect.context(context, _task_name=self.task.name):

            try:
                # retrieve the run number and place in context
                state = self.get_run_count(state=state)

                # check if all upstream tasks have finished
                state = self.check_upstream_finished(
                    state, upstream_states_set=upstream_states_set
                )

                # check if any upstream tasks skipped (and if we need to skip)
                state = self.check_upstream_skipped(
                    state, upstream_states_set=upstream_states_set
                )

                # check if the task's trigger passes
                state = self.check_task_trigger(
                    state,
                    upstream_states_set=upstream_states_set,
                    ignore_trigger=ignore_trigger
                    | mapped,  # the children of a mapped task are responsible for checking their triggers
                )

                # check to make sure the task is in a pending state
                state = self.check_task_is_pending(state)

                # check to see if the task has a cached result
                state = self.check_task_is_cached(state, inputs=task_inputs)

                # set the task state to running
                state = self.set_task_to_running(state)

                # run the task!
                if not mapped:
                    state = self.get_task_run_state(
                        state, inputs=task_inputs, timeout_handler=timeout_handler
                    )

                    # cache the output, if appropriate
                    state = self.cache_result(state, inputs=task_inputs)

                # check if the task needs to be retried
                state = self.check_for_retry(state, inputs=task_inputs)

                # check if the task is ready to be mapped
                state = self.check_for_mapped(
                    state, upstream_states=upstream_states, mapped=mapped
                )

            # a ENDRUN signal at any point breaks the chain and we return
            # the most recently computed state
            except ENDRUN as exc:
                state = exc.state

            except signals.PAUSE as exc:
                state.cached_inputs = task_inputs or {}
                state.message = exc

            finally:  # resource is now available
                for ticket, q in zip(tickets, queues):
                    q.put(ticket)

        return state

    @call_state_handlers
    def get_run_count(self, state: State) -> State:
        """
        If the task is being retried, then we retrieve the run count from the initial Retry
        state. Otherwise, we assume the run count is 1. The run count is stored in context as
        _task_run_count.

        Args:
            - state (State): the current state of the task

        Returns:
            State: the state of the task after running the check
        """
        if isinstance(state, Retrying):
            run_count = state.run_count + 1
        else:
            run_count = 1
        prefect.context.update(_task_run_count=run_count)
        return state

    @call_state_handlers
    def check_upstream_finished(
        self, state: State, upstream_states_set: Set[State]
    ) -> State:
        """
        Checks if the upstream tasks have all finshed.

        Args:
            - state (State): the current state of this task
            - upstream_states_set: a set containing the states of any upstream tasks.

        Returns:
            State: the state of the task after running the check

        Raises:
            - ENDRUN: if upstream tasks are not finished.
        """
        if not all(s.is_finished() for s in upstream_states_set):
            raise ENDRUN(state)
        return state

    @call_state_handlers
    def check_upstream_skipped(
        self, state: State, upstream_states_set: Set[State]
    ) -> State:
        """
        Checks if any of the upstream tasks have skipped.

        Args:
            - state (State): the current state of this task
            - upstream_states_set: a set containing the states of any upstream tasks.

        Returns:
            State: the state of the task after running the check
        """
        if self.task.skip_on_upstream_skip and any(
            s.is_skipped() for s in upstream_states_set
        ):
            raise ENDRUN(
                state=Skipped(
                    message=(
                        "Upstream task was skipped; if this was not the intended "
                        "behavior, consider changing `skip_on_upstream_skip=False` "
                        "for this task."
                    )
                )
            )
        return state

    @call_state_handlers
    def check_task_trigger(
        self,
        state: State,
        upstream_states_set: Set[State],
        ignore_trigger: bool = False,
    ) -> State:
        """
        Checks if the task's trigger function passes. If the upstream_states_set is empty,
        then the trigger is not called.

        Args:
            - state (State): the current state of this task
            - upstream_states_set (Set[State]): a set containing the states of any upstream tasks.
            - ignore_trigger (bool): a boolean indicating whether to ignore the
                tasks's trigger

        Returns:
            State: the state of the task after running the check

        Raises:
            - ENDRUN: if the trigger raises an error
        """
        # the trigger itself could raise a failure, but we raise TriggerFailed just in case
        raise_on_exception = prefect.context.get("_raise_on_exception", False)

        try:
            if not upstream_states_set:
                return state
            elif not ignore_trigger and not self.task.trigger(upstream_states_set):
                raise signals.TRIGGERFAIL(message="Trigger failed")

        except signals.PAUSE:
            raise

        except signals.PrefectStateSignal as exc:
            logging.debug("{} signal raised.".format(type(exc).__name__))
            if raise_on_exception:
                raise exc
            raise ENDRUN(exc.state)

        # Exceptions are trapped and turned into TriggerFailed states
        except Exception as exc:
            logging.debug("Unexpected error while running task.")
            if raise_on_exception:
                raise exc
            raise ENDRUN(TriggerFailed(message=exc))

        return state

    @call_state_handlers
    def check_task_is_pending(self, state: State) -> State:
        """
        Checks to make sure the task is in a PENDING state.

        Args:
            - state (State): the current state of this task

        Returns:
            State: the state of the task after running the check

        Raises:
            - ENDRUN: if the task is not ready to run
        """
        # the task is ready
        if state.is_pending():
            return state

        # this task is already running
        elif state.is_running():
            self.logger.debug("Task is already running.")
            raise ENDRUN(state)

        # this task is already finished
        elif state.is_finished():
            self.logger.debug("Task is already finished.")
            raise ENDRUN(state)

        # this task is not pending
        else:
            self.logger.debug(
                "Task is not ready to run or state was unrecognized ({}).".format(state)
            )
            raise ENDRUN(state)

    @call_state_handlers
    def check_task_is_cached(self, state: State, inputs: Dict[str, Any]) -> State:
        """
        Args:
            - state (State): the current state of this task
            - inputs (Dict[str, Any]): a dictionary of inputs whose keys correspond
                to the task's `run()` arguments.

        Returns:
            State: the state of the task after running the check

        Raises:
            - ENDRUN: if the task is not ready to run
        """
        if isinstance(state, CachedState) and self.task.cache_validator(
            state, inputs, prefect.context.get("_parameters")
        ):
            raise ENDRUN(Success(result=state.cached_result, cached=state))
        return state

    @call_state_handlers
    def check_for_mapped(
        self,
        state: State,
        upstream_states: Dict[Edge, Union[State, List[State]]],
        mapped: bool,
    ) -> State:
        """
        If the task is being mapepd, sets the task to `Mapped`

        Args:
            - state (State): the current state of this task
            - upstream_states (Dict[Edge, Union[State, List[State]]]): a dictionary
                representing the states of any tasks upstream of this one. The keys of the
                dictionary should correspond to the edges leading to the task.
            - mapped (bool): whether this task is to be mapped

        Returns:
            State: the state of the task after running the check

        Raises:
            - ENDRUN: if the task is not ready to be mapped
        """
        if not state.is_running():
            raise ENDRUN(state)

        if not mapped:
            return state

        mapped_upstreams = [val for e, val in upstream_states.items() if e.mapped]

        ## no inputs provided
        if not mapped_upstreams:
            raise ENDRUN(state=Skipped(message="No inputs provided to map over."))

        iterable_values = []
        for value in mapped_upstreams:
            underlying = value if not isinstance(value, State) else value.result
            iterable_values.append(underlying)

        ## check that all upstream values are iterable
        if any([not isinstance(v, collections.abc.Iterable) for v in iterable_values]):
            raise ENDRUN(
                state=Failed(
                    message="Non-iterable upstream values cannot be mapped over."
                )
            )

        ## check that no upstream values are empty
        if any([len(v) == 0 for v in iterable_values]):
            raise ENDRUN(state=Skipped(message="Empty inputs provided to map over."))
        return Mapped(message="Task ready to be mapped.")

    @call_state_handlers
    def set_task_to_running(self, state: State) -> State:
        """
        Sets the task to running

        Args:
            - state (State): the current state of this task

        Returns:
            State: the state of the task after running the check

        Raises:
            - ENDRUN: if the task is not ready to run
        """
        if not state.is_pending():
            raise ENDRUN(state)

        return Running(message="Starting task run.")

    @call_state_handlers
    def get_task_run_state(
        self, state: State, inputs: Dict[str, Any], timeout_handler: Optional[Callable]
    ) -> State:
        """
        Runs the task and traps any signals or errors it raises.

        Args:
            - state (State): the current state of this task
            - inputs (Dict[str, Any], optional): a dictionary of inputs whose keys correspond
                to the task's `run()` arguments.
            - timeout_handler (Callable, optional): function for timing out
                task execution, with call signature `handler(fn, *args, **kwargs)`. Defaults to
                `prefect.utilities.executors.main_thread_timeout`

        Returns:
            State: the state of the task after running the check

        Raises:
            - signals.PAUSE: if the task raises PAUSE
            - ENDRUN: if the task is not ready to run
        """
        if not state.is_running():
            raise ENDRUN(state)

        raise_on_exception = prefect.context.get("_raise_on_exception", False)

        try:
            timeout_handler = timeout_handler or main_thread_timeout
            result = timeout_handler(self.task.run, timeout=self.task.timeout, **inputs)

        except signals.PAUSE:
            raise

        # PrefectStateSignals are trapped and turned into States
        except signals.PrefectStateSignal as exc:
            logging.debug("{} signal raised.".format(type(exc).__name__))
            if raise_on_exception:
                raise exc
            return exc.state

        # Exceptions are trapped and turned into Failed states
        except Exception as exc:
            logging.debug("Unexpected error while running task.")
            if raise_on_exception:
                raise exc
            return Failed(message=exc)

        return Success(result=result, message="Task run succeeded.")

    def cache_result(self, state: State, inputs: Dict[str, Any]) -> State:
        """
        Caches the result of a successful task, if appropriate.

        Tasks are cached if:
            - task.cache_for is not None
            - the task state is Successful
            - the task state is not Skipped (which is a subclass of Successful)

        Args:
            - state (State): the current state of this task
            - inputs (Dict[str, Any], optional): a dictionary of inputs whose keys correspond
                to the task's `run()` arguments.

        Returns:
            State: the state of the task after running the check

        """
        if (
            state.is_successful()
            and not state.is_skipped()
            and self.task.cache_for is not None
        ):
            expiration = datetime.datetime.utcnow() + self.task.cache_for
            cached_state = CachedState(
                cached_inputs=inputs,
                cached_result_expiration=expiration,
                cached_parameters=prefect.context.get("_parameters"),
                cached_result=state.result,
            )
            return Success(
                result=state.result, message=state.message, cached=cached_state
            )

        return state

    @call_state_handlers
    def check_for_retry(self, state: State, inputs: Dict[str, Any]) -> State:
        """
        Checks to see if a FAILED task should be retried.

        Args:
            - state (State): the current state of this task
            - inputs (Dict[str, Any], optional): a dictionary of inputs whose keys correspond
                to the task's `run()` arguments.

        Returns:
            State: the state of the task after running the check
        """
        if state.is_failed():
            run_count = prefect.context.get("_task_run_count", 1)
            if run_count <= self.task.max_retries:
                start_time = datetime.datetime.utcnow() + self.task.retry_delay
                msg = "Retrying Task (after attempt {n} of {m})".format(
                    n=run_count, m=self.task.max_retries + 1
                )
                return Retrying(
                    start_time=start_time,
                    cached_inputs=inputs,
                    message=msg,
                    run_count=run_count,
                )

        return state
