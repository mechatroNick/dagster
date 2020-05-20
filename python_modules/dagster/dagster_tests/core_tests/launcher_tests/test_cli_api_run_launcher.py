import os
import shutil
import time
import uuid
from contextlib import contextmanager

from dagster import RepositoryDefinition, file_relative_path, pipeline, seven, solid
from dagster.core.definitions.reconstructable import ReconstructableRepository
from dagster.core.host_representation import EnvironmentHandle, ExternalRepository, RepositoryHandle
from dagster.core.instance import DagsterInstance
from dagster.core.launcher import CliApiRunLauncher
from dagster.core.storage.pipeline_run import PipelineRunStatus
from dagster.seven import get_system_temp_directory
from dagster.utils import mkdir_p


@solid
def noop_solid(_):
    pass


@pipeline
def noop_pipeline():
    pass


@solid
def crashy_solid(_):
    os._exit(1)  # pylint: disable=W0212


@pipeline
def crashy_pipeline():
    crashy_solid()


@solid
def sleepy_solid(_):
    while True:
        time.sleep(0.1)


@pipeline
def sleepy_pipeline():
    sleepy_solid()


def define_repository():
    return RepositoryDefinition(
        name='nope', pipeline_defs=[noop_pipeline, crashy_pipeline, sleepy_pipeline, math_diamond]
    )


@contextmanager
def temp_instance():
    system_temp = get_system_temp_directory()

    tmp_path = os.path.join(system_temp, str(uuid.uuid4()))
    mkdir_p(tmp_path)
    try:
        yield DagsterInstance.local_temp(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                # sometimes this fails, but seemingly only on windows
                shutil.rmtree(tmp_path)
            except seven.FileNotFoundError:
                pass


def test_repo_construction():
    repo_yaml = file_relative_path(__file__, 'repo.yaml')
    assert ReconstructableRepository.from_yaml(repo_yaml).get_definition()


def get_external_repo(repo_yaml):
    defn = ReconstructableRepository.from_yaml(repo_yaml).get_definition()
    return ExternalRepository.from_repository_def(
        defn, RepositoryHandle(defn.name, EnvironmentHandle.legacy_from_yaml('test', repo_yaml))
    )


def get_full_external_pipeline(repo_yaml, pipeline_name):
    return get_external_repo(repo_yaml).get_full_external_pipeline(pipeline_name)


def test_successful_run():
    with temp_instance() as instance:
        repo_yaml = file_relative_path(__file__, 'repo.yaml')
        pipeline_run = instance.create_run_for_pipeline(
            pipeline_def=noop_pipeline, environment_dict=None
        )

        external_pipeline = get_full_external_pipeline(repo_yaml, pipeline_run.pipeline_name)

        run_id = pipeline_run.run_id

        assert instance.get_run_by_id(run_id).status == PipelineRunStatus.NOT_STARTED

        launcher = CliApiRunLauncher()
        launcher.launch_run(
            instance=instance, run=pipeline_run, external_pipeline=external_pipeline
        )
        launcher.join()

        finished_pipeline_run = instance.get_run_by_id(run_id)

        assert finished_pipeline_run
        assert finished_pipeline_run.run_id == run_id
        assert finished_pipeline_run.status == PipelineRunStatus.SUCCESS


def test_crashy_run():

    with temp_instance() as instance:
        repo_yaml = file_relative_path(__file__, 'repo.yaml')
        pipeline_run = instance.create_run_for_pipeline(
            pipeline_def=crashy_pipeline, environment_dict=None
        )
        run_id = pipeline_run.run_id

        assert instance.get_run_by_id(run_id).status == PipelineRunStatus.NOT_STARTED

        external_pipeline = get_full_external_pipeline(repo_yaml, pipeline_run.pipeline_name)

        launcher = CliApiRunLauncher()
        launcher.launch_run(instance, pipeline_run, external_pipeline)

        time.sleep(2)

        launcher.join()

        failed_pipeline_run = instance.get_run_by_id(run_id)

        assert failed_pipeline_run
        assert failed_pipeline_run.run_id == run_id
        assert failed_pipeline_run.status == PipelineRunStatus.FAILURE

        event_records = instance.all_logs(run_id)

        message = 'Pipeline execution process for {run_id} unexpectedly exited.'.format(
            run_id=run_id
        )

        assert _message_exists(event_records, message)


def test_terminated_run():
    with temp_instance() as instance:
        repo_yaml = file_relative_path(__file__, 'repo.yaml')
        pipeline_run = instance.create_run_for_pipeline(
            pipeline_def=sleepy_pipeline, environment_dict=None
        )
        run_id = pipeline_run.run_id

        assert instance.get_run_by_id(run_id).status == PipelineRunStatus.NOT_STARTED

        external_pipeline = get_full_external_pipeline(repo_yaml, pipeline_run.pipeline_name)
        launcher = CliApiRunLauncher()
        launcher.launch_run(instance, pipeline_run, external_pipeline)

        time.sleep(0.5)

        assert launcher.can_terminate(run_id)
        launcher.terminate(run_id)

        launcher.join()

        terminated_pipeline_run = instance.get_run_by_id(run_id)
        assert terminated_pipeline_run.status == PipelineRunStatus.FAILURE


def _get_engine_events(event_records):
    for er in event_records:
        if er.dagster_event and er.dagster_event.is_engine_event:
            yield er


def _message_exists(event_records, message_text):
    for event_record in event_records:
        if message_text in event_record.message:
            return True

    return False


@solid
def return_one(_):
    return 1


@solid
def multiply_by_2(_, num):
    return num * 2


@solid
def multiply_by_3(_, num):
    return num * 3


@solid
def add(_, num1, num2):
    return num1 + num2


@pipeline
def math_diamond():
    one = return_one()
    add(multiply_by_2(one), multiply_by_3(one))


def test_engine_events():

    with temp_instance() as instance:
        repo_yaml = file_relative_path(__file__, 'repo.yaml')

        pipeline_run = instance.create_run_for_pipeline(
            pipeline_def=math_diamond, environment_dict=None
        )
        run_id = pipeline_run.run_id

        assert instance.get_run_by_id(run_id).status == PipelineRunStatus.NOT_STARTED

        external_pipeline = get_full_external_pipeline(repo_yaml, pipeline_run.pipeline_name)
        launcher = CliApiRunLauncher()
        launcher.launch_run(instance, pipeline_run, external_pipeline)
        launcher.join()

        finished_pipeline_run = instance.get_run_by_id(run_id)

        assert finished_pipeline_run
        assert finished_pipeline_run.run_id == run_id
        assert finished_pipeline_run.status == PipelineRunStatus.SUCCESS
        event_records = instance.all_logs(run_id)

        about_to_start, started_process, executing_steps, finished_steps, process_exited = tuple(
            _get_engine_events(event_records)
        )

        assert 'About to start process' in about_to_start.message
        assert 'Started process for pipeline' in started_process.message
        assert 'Executing steps in process' in executing_steps.message
        assert 'Finished steps in process' in finished_steps.message
        assert 'Process for pipeline exited' in process_exited.message