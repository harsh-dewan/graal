#
# Copyright (c) 2018, 2021, Oracle and/or its affiliates. All rights reserved.
# DO NOT ALTER OR REMOVE COPYRIGHT NOTICES OR THIS FILE HEADER.
#
# This code is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 2 only, as
# published by the Free Software Foundation.  Oracle designates this
# particular file as subject to the "Classpath" exception as provided
# by Oracle in the LICENSE file that accompanied this code.
#
# This code is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# version 2 for more details (a copy is included in the LICENSE file that
# accompanied this code).
#
# You should have received a copy of the GNU General Public License version
# 2 along with this work; if not, write to the Free Software Foundation,
# Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Please contact Oracle, 500 Oracle Parkway, Redwood Shores, CA 94065 USA
# or visit www.oracle.com if you need additional information or have any
# questions.
#
from __future__ import annotations

import datetime
import os
import re
import shutil
import tempfile
import json
from genericpath import exists
from os.path import basename, dirname, getsize
from traceback import print_tb
import subprocess
import zipfile
from typing import Iterable, Optional

import mx
import mx_benchmark
import mx_sdk_benchmark
import mx_util
import mx_sdk_vm
import mx_sdk_vm_impl
from mx_sdk_vm_impl import svm_experimental_options
from mx_benchmark import DataPoint, DataPoints, BenchmarkSuite
from mx_sdk_benchmark import StagesInfo, NativeImageBenchmarkMixin

_suite = mx.suite('vm')
_polybench_vm_registry = mx_benchmark.VmRegistry('PolyBench', 'polybench-vm')
_polybench_modes = [
    ('standard', ['--mode=standard']),
    ('interpreter', ['--mode=interpreter']),
]

POLYBENCH_METRIC_MAPPING = {
    "compilation-time": "compile-time",
    "partial-evaluation-time": "pe-time",
    "allocated-bytes": "allocated-memory",
    "peak-time": "time"
}  # Maps some polybench metrics to standardized metric names


class GraalVm(mx_benchmark.OutputCapturingJavaVm):
    def __init__(self, name, config_name, extra_java_args, extra_launcher_args):
        """
        :type name: str
        :type config_name: str
        :type extra_java_args: list[str] | None
        :type extra_launcher_args: list[str] | None
        """
        super(GraalVm, self).__init__()
        self._name = name
        self._config_name = config_name
        self.extra_java_args = extra_java_args or []
        self.extra_launcher_args = extra_launcher_args or []
        self.debug_args = mx.java_debug_args() if "jvm" in config_name else []

    def name(self):
        return self._name

    def config_name(self):
        return self._config_name

    def post_process_command_line_args(self, args):
        return self.extra_java_args + self.debug_args + args

    def post_process_launcher_command_line_args(self, args):
        return self.extra_launcher_args + \
               ['--vm.' + x[1:] if x.startswith('-X') else x for x in self.debug_args] + \
               args

    def home(self):
        if self.name() == 'native-image-java-home':
            return mx.get_jdk().home
        return mx_sdk_vm_impl.graalvm_home(fatalIfMissing=True)

    def generate_java_command(self, args):
        return [os.path.join(self.home(), 'bin', 'java')] + args

    def run_java(self, args, out=None, err=None, cwd=None, nonZeroIsFatal=False):
        """Run 'java' workloads."""
        self.extract_vm_info(args)
        cmd = self.generate_java_command(args)
        cmd = mx.apply_command_mapper_hooks(cmd, self.command_mapper_hooks)
        return mx.run(cmd, out=out, err=err, cwd=cwd, nonZeroIsFatal=nonZeroIsFatal)

    def run_lang(self, cmd, args, cwd):
        """Deprecated. Call 'run_launcher' instead."""
        mx.log_deprecation("'run_lang' is deprecated. Use 'run_launcher' instead.")
        return self.run_launcher(cmd, args, cwd)

    def run_launcher(self, cmd, args, cwd):
        """Run the 'cmd' command in the 'bin' directory."""
        args = self.post_process_launcher_command_line_args(args)
        self.extract_vm_info(args)
        mx.log(f"Running '{cmd}' on '{self.name()}' with args: '{' '.join(args)}'")
        out = mx.TeeOutputCapture(mx.OutputCapture())
        command = [os.path.join(self.home(), 'bin', cmd)] + args
        command = mx.apply_command_mapper_hooks(command, self.command_mapper_hooks)
        code = mx.run(command, out=out, err=out, cwd=cwd, nonZeroIsFatal=False)
        out = out.underlying.data
        dims = self.dimensions(cwd, args, code, out)
        return code, out, dims


class NativeImageBenchmarkConfig:
    def __init__(self, vm: "NativeImageVM", bm_suite: BenchmarkSuite | NativeImageBenchmarkMixin, args):
        self.bm_suite = bm_suite
        self.benchmark_suite_name = bm_suite.benchSuiteName(args)
        self.benchmark_name = bm_suite.benchmarkName()
        self.executable, self.classpath_arguments, self.modulepath_arguments, self.system_properties, self.image_vm_args, image_run_args, self.split_run = NativeImageVM.extract_benchmark_arguments(args)
        self.extra_image_build_arguments = bm_suite.extra_image_build_argument(self.benchmark_name, args)
        # use list() to create fresh copies to safeguard against accidental modification
        self.image_run_args = bm_suite.extra_run_arg(self.benchmark_name, args, list(image_run_args))
        self.extra_jvm_args = bm_suite.extra_jvm_arg(self.benchmark_name, args)
        self.extra_agent_run_args = bm_suite.extra_agent_run_arg(self.benchmark_name, args, list(image_run_args))
        self.extra_agentlib_options = bm_suite.extra_agentlib_options(self.benchmark_name, args, list(image_run_args))
        for option in self.extra_agentlib_options:
            if option.startswith('config-output-dir'):
                mx.abort("config-output-dir must not be set in the extra_agentlib_options.")
        # Do not strip the run arguments if safepoint-sampler configuration is active.
        self.extra_profile_run_args = bm_suite.extra_profile_run_arg(self.benchmark_name, args, list(image_run_args), not vm.safepoint_sampler)
        self.extra_agent_profile_run_args = bm_suite.extra_agent_profile_run_arg(self.benchmark_name, args, list(image_run_args))
        self.benchmark_output_dir = bm_suite.benchmark_output_dir(self.benchmark_name, args)
        self.params = ['extra-image-build-argument', 'extra-jvm-arg', 'extra-run-arg', 'extra-agent-run-arg', 'extra-profile-run-arg',
                       'extra-agent-profile-run-arg', 'benchmark-output-dir', 'stages', 'skip-agent-assertions']

        # These stages are not executed, even if explicitly requested.
        # Some configurations don't need to/can't run certain stages
        removed_stages = set()

        if vm.jdk_profiles_collect:
            # forbid image build/run in the profile collection execution mode
            removed_stages.update(["image", "run"])
        if vm.profile_inference_feature_extraction:
            # do not run the image in the profile inference feature extraction mode
            removed_stages.add("run")
        self.skip_agent_assertions = bm_suite.skip_agent_assertions(self.benchmark_name, args)
        self.root_dir = self.benchmark_output_dir if self.benchmark_output_dir else mx.suite('vm').get_output_root(platformDependent=False, jdkDependent=False)
        unique_suite_name = f"{self.bm_suite.benchSuiteName()}-{self.bm_suite.version().replace('.', '-')}" if self.bm_suite.version() != 'unknown' else self.bm_suite.benchSuiteName()
        self.executable_name = (unique_suite_name + '-' + self.benchmark_name).lower() if self.benchmark_name else unique_suite_name.lower()
        self.instrumentation_executable_name = self.executable_name + "-instrument"
        self.final_image_name = self.executable_name + '-' + vm.config_name()
        self.output_dir = os.path.join(os.path.abspath(self.root_dir), 'native-image-benchmarks', self.executable_name + '-' + vm.config_name())
        self.profile_path = os.path.join(self.output_dir, self.executable_name) + ".iprof"
        self.config_dir = os.path.join(self.output_dir, 'config')
        self.log_dir = self.output_dir
        base_image_build_args = ['--no-fallback', '-g']
        base_image_build_args += ['-H:+VerifyGraalGraphs', '-H:+VerifyPhases', '--diagnostics-mode'] if vm.is_gate else []
        base_image_build_args += ['-H:+ReportExceptionStackTraces']
        base_image_build_args += bm_suite.build_assertions(self.benchmark_name, vm.is_gate)

        base_image_build_args += self.system_properties
        self.bundle_path = self.get_bundle_path_if_present()
        self.bundle_create_path = self.get_bundle_create_path_if_present()
        if not self.bundle_path:
            base_image_build_args += self.classpath_arguments
            base_image_build_args += self.modulepath_arguments
            base_image_build_args += self.executable
            base_image_build_args += ['-H:Path=' + self.output_dir]
        base_image_build_args += [
            '-H:ConfigurationFileDirectories=' + self.config_dir,
            '-H:+PrintAnalysisStatistics',
            '-H:+PrintCallEdges',
            '-H:+CollectImageBuildStatistics',
        ]
        self.image_build_reports_directory = os.path.join(self.output_dir, 'reports')
        if self.bundle_create_path is not None:
            self.image_build_reports_directory = os.path.join(self.output_dir, self.bundle_create_path)
        self.image_build_stats_file = os.path.join(self.image_build_reports_directory, 'image_build_statistics.json')

        if vm.is_quickbuild:
            base_image_build_args += ['-Ob']
        if vm.use_string_inlining:
            base_image_build_args += ['-H:+UseStringInlining']
        if vm.is_llvm:
            base_image_build_args += ['--features=org.graalvm.home.HomeFinderFeature'] + ['-H:CompilerBackend=llvm', '-H:DeadlockWatchdogInterval=0']
        if vm.gc:
            base_image_build_args += ['--gc=' + vm.gc] + ['-H:+SpawnIsolates']
        if vm.native_architecture:
            base_image_build_args += ['-march=native']
        if vm.analysis_context_sensitivity:
            base_image_build_args += ['-H:AnalysisContextSensitivity=' + vm.analysis_context_sensitivity, '-H:-RemoveSaturatedTypeFlows', '-H:+AliasArrayTypeFlows']
        if vm.no_inlining_before_analysis:
            base_image_build_args += ['-H:-InlineBeforeAnalysis']
        if vm.optimization_level:
            base_image_build_args += ['-' + vm.optimization_level]
        if vm.async_sampler:
            base_image_build_args += ['-R:+FlightRecorder',
                                      '-R:StartFlightRecording=filename=default.jfr',
                                      '--enable-monitoring=jfr',
                                      '-R:+JfrBasedExecutionSamplerStatistics'
                                      ]
            removed_stages.update(["instrument-image", "instrument-run"])
        if not vm.pgo_instrumentation:
            removed_stages.update(["instrument-image", "instrument-run"])
        if self.image_vm_args is not None:
            base_image_build_args += self.image_vm_args
        self.is_runnable = self.check_runnable()
        base_image_build_args += self.extra_image_build_arguments

        # Inform the StagesInfo object about removed stages
        bm_suite.stages_info.setup(removed_stages)

        # benchmarks are allowed to use experimental options
        self.base_image_build_args = [os.path.join(vm.home(), 'bin', 'native-image')] + svm_experimental_options(base_image_build_args)

    def check_runnable(self):
        # TODO remove once there is load available for the specified benchmarks
        if self.benchmark_suite_name in ["mushop", "quarkus"]:
            return False
        return True

    def get_bundle_path_if_present(self):
        bundle_apply_arg = "--bundle-apply="
        for i in range(len(self.extra_image_build_arguments)):
            if self.extra_image_build_arguments[i].startswith(bundle_apply_arg):
                # The bundle output is produced next to the bundle file, which in the case of
                # benchmarks is in the mx cache, so we make a local copy.
                cached_bundle_path = self.extra_image_build_arguments[i][len(bundle_apply_arg):]
                bundle_copy_path = os.path.join(self.output_dir, basename(cached_bundle_path))
                mx_util.ensure_dirname_exists(bundle_copy_path)
                mx.copyfile(cached_bundle_path, bundle_copy_path)
                self.extra_image_build_arguments[i] = bundle_apply_arg + bundle_copy_path
                return bundle_copy_path

        return None

    def get_bundle_create_path_if_present(self):
        bundle_create_arg = "--bundle-create"
        bundle_arg_idx = [idx for idx, arg in enumerate(self.extra_image_build_arguments) if arg.startswith(bundle_create_arg)]
        if len(bundle_arg_idx) == 1:
            bp = os.path.join(self.extra_image_build_arguments[bundle_arg_idx[0] + 1] + ".output", "default", "reports")
            return bp

        return None


class NativeImageStages:
    def __init__(self, stages_info: StagesInfo, config, bench_out, bench_err, is_gate, non_zero_is_fatal, cwd):
        self.stages_info = stages_info
        self.config: NativeImageBenchmarkConfig = config
        self.bench_out = bench_out
        self.bench_err = bench_err
        self.final_image_name = config.final_image_name
        self.is_gate = is_gate
        self.non_zero_is_fatal = non_zero_is_fatal
        self.cwd = cwd

        self.exit_code = None
        self.command = None
        self.stderr_path = None
        self.stdout_path = None

    def reset_stage(self):
        self.exit_code = None
        self.command = None
        self.stderr_path = None
        self.stdout_path = None

    def __enter__(self):
        self.stdout_path = os.path.abspath(os.path.join(self.config.log_dir, self.final_image_name + '-' + self.stages_info.requested_stage + '-stdout.log'))
        self.stderr_path = os.path.abspath(os.path.join(self.config.log_dir, self.final_image_name + '-' + self.stages_info.requested_stage + '-stderr.log'))
        self.stdout_file = open(self.stdout_path, 'w')
        self.stderr_file = open(self.stderr_path, 'w')

        self.separator_line()
        mx.log(self.get_timestamp() + 'Entering stage: ' + self.stages_info.requested_stage + ' for ' + self.final_image_name)
        self.separator_line()

        mx.log('Running: ')
        mx.log(' '.join(self.command))

        if self.stdout_path:
            mx.log('The standard output is saved to ' + str(self.stdout_path))
        if self.stderr_path:
            mx.log('The standard error is saved to ' + str(self.stderr_path))

        return self

    def __exit__(self, tp, value, tb):
        self.stdout_file.flush()
        self.stderr_file.flush()

        if self.exit_code == 0 and (tb is None):
            self.stages_info.success()
            if self.config.split_run:
                with open(self.config.split_run, 'a') as stdout:
                    stdout.write(self.get_timestamp() + self.config.bm_suite.name() + ':' + self.config.benchmark_name + ' ' + self.stages_info.requested_stage + ': PASS\n')
            if self.stages_info.requested_stage == self.stages_info.last_stage:
                self.bench_out(f"{self.get_timestamp()}{mx_sdk_benchmark.STAGE_LAST_SUCCESSFUL_PREFIX} {self.stages_info.requested_stage} for {self.final_image_name}")
            else:
                self.bench_out(f"{self.get_timestamp()}{mx_sdk_benchmark.STAGE_SUCCESSFUL_PREFIX} {self.stages_info.requested_stage}")

            self.separator_line()
        else:
            self.stages_info.fail()
            if self.config.split_run:
                with open(self.config.split_run, 'a') as stdout:
                    stdout.write(self.get_timestamp() + self.config.bm_suite.name() + ':' + self.config.benchmark_name + ' ' + self.stages_info.requested_stage + ': FAILURE\n')
            if self.exit_code is not None and self.exit_code != 0:
                mx.log(mx.colorize(self.get_timestamp() + 'Failed in stage ' + self.stages_info.requested_stage + ' for ' + self.final_image_name + ' with exit code ' + str(self.exit_code), 'red'))

            if self.stdout_path:
                mx.log(mx.colorize('--------- Standard output:', 'blue'))
                with open(self.stdout_path, 'r') as stdout:
                    mx.log(stdout.read())

            if self.stderr_path:
                mx.log(mx.colorize('--------- Standard error:', 'red'))
                with open(self.stderr_path, 'r') as stderr:
                    mx.log(stderr.read())

            if tb:
                mx.log(mx.colorize(self.get_timestamp() + 'Failed in stage ' + self.stages_info.requested_stage + ' with ', 'red'))
                print_tb(tb)

            self.separator_line()

            mx.log(mx.colorize('--------- To run the failed benchmark execute the following: ', 'green'))
            mx.log(mx.current_mx_command())

            if self.stages_info.stages_till_now:
                mx.log(mx.colorize('--------- To only prepare the benchmark add the following to the end of the previous command: ', 'green'))
                mx.log('-Dnative-image.benchmark.stages=' + ','.join(self.stages_info.stages_till_now))

            mx.log(mx.colorize('--------- To only run the failed stage add the following to the end of the previous command: ', 'green'))
            mx.log('-Dnative-image.benchmark.stages=' + self.stages_info.requested_stage)

            mx.log(mx.colorize('--------- Additional arguments that can be used for debugging the benchmark go after the final --: ', 'green'))
            for param in self.config.params:
                mx.log('-Dnative-image.benchmark.' + param + '=')

            self.separator_line()
            if self.non_zero_is_fatal:
                mx.abort(self.get_timestamp() + 'Exiting the benchmark due to the failure.')

        self.stdout_file.close()
        self.stderr_file.close()
        self.reset_stage()

    def stdout(self, include_bench_out=False):
        def writeFun(s):
            v = self.stdout_file.write(s)
            if include_bench_out:
                self.bench_out(s)
            else:
                mx.log(s, end='')
            return v
        return writeFun

    def stderr(self, include_bench_err=False):
        def writeFun(s):
            v = self.stdout_file.write(s)
            if include_bench_err:
                self.bench_err(s)
            else:
                mx.log(s, end='')
            return v
        return writeFun

    @staticmethod
    def separator_line():
        mx.log(mx.colorize('-' * 120, 'green'))

    @staticmethod
    def get_timestamp():
        return '[' + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + '] '

    def set_command(self, command):
        self.command = command
        return self

    def execute_command(self, vm=None):
        write_output = self.stages_info.effective_stage in ["run", "image"] or self.is_gate

        cmd = self.command
        self.exit_code = self.config.bm_suite.run_stage(vm, self.stages_info.effective_stage, cmd, self.stdout(write_output), self.stderr(write_output), self.cwd, False)
        if "image" not in self.stages_info.effective_stage and self.config.bm_suite.validateReturnCode(self.exit_code):
            self.exit_code = 0


class NativeImageVM(GraalVm):
    """
    A VM implementation to build and run Native Image benchmarks.

    Runs individual stages of the benchmarking process (or all stages in sequence in the fallback mode).
    See also :class:`NativeImageBenchmarkMixin` for more information on the Native Image benchmarking process.
    """

    def __init__(self, name, config_name, extra_java_args=None, extra_launcher_args=None):
        super().__init__(name, config_name, extra_java_args, extra_launcher_args)
        self.vm_args = None
        self.pgo_instrumentation = False
        self.pgo_context_sensitive = True
        self.is_gate = False
        self.is_quickbuild = False
        self.use_string_inlining = False
        self.is_llvm = False
        self.gc = None
        self.native_architecture = False
        self.use_upx = False
        self.graalvm_edition = None
        self.config: Optional[NativeImageBenchmarkConfig] = None
        self.stages_info: Optional[StagesInfo] = None
        self.stages: Optional[NativeImageStages] = None
        self.jdk_profiles_collect = False
        self.adopted_jdk_pgo = False
        self.async_sampler = False
        self.safepoint_sampler = False
        self.profile_inference_feature_extraction = False
        self.analysis_context_sensitivity = None
        self.no_inlining_before_analysis = False
        self.optimization_level = None
        self._configure_from_name(config_name)

    def _configure_from_name(self, config_name):
        if not config_name:
            mx.abort(f"config_name must be set. Use 'default' for the default {self.__class__.__name__} configuration.")

        # special case for the 'default' configuration, other configurations are handled by the regex to ensure consistent ordering
        if config_name == "default":
            return
        if config_name == "default-ce":
            self.graalvm_edition = "ce"
            return
        if config_name == "default-ee":
            self.graalvm_edition = "ee"
            return

        # This defines the allowed config names for NativeImageVM. The ones registered will be available via --jvm-config
        rule = r'^(?P<native_architecture>native-architecture-)?(?P<string_inlining>string-inlining-)?(?P<gate>gate-)?(?P<upx>upx-)?(?P<quickbuild>quickbuild-)?(?P<gc>g1gc-)?(?P<llvm>llvm-)?(?P<pgo>pgo-|pgo-ctx-insens-)?(?P<inliner>inline-)?' \
               r'(?P<analysis_context_sensitivity>insens-|allocsens-|1obj-|2obj1h-|3obj2h-|4obj3h-)?(?P<no_inlining_before_analysis>no-inline-)?(?P<jdk_profiles>jdk-profiles-collect-|adopted-jdk-pgo-)?' \
               r'(?P<profile_inference>profile-inference-feature-extraction-)?(?P<sampler>safepoint-sampler-|async-sampler-)?(?P<optimization_level>O0-|O1-|O2-|O3-)?(?P<edition>ce-|ee-)?$'

        mx.logv(f"== Registering configuration: {config_name}")
        match_name = f"{config_name}-"  # adding trailing dash to simplify the regex
        matching = re.match(rule, match_name)
        if not matching:
            mx.abort(f"{self.__class__.__name__} configuration is invalid: {config_name}")

        if matching.group("native_architecture") is not None:
            mx.logv(f"'native-architecture' is enabled for {config_name}")
            self.native_architecture = True

        if matching.group("string_inlining") is not None:
            mx.logv(f"'string-inlining' is enabled for {config_name}")
            self.use_string_inlining = True

        if matching.group("gate") is not None:
            mx.logv(f"'gate' mode is enabled for {config_name}")
            self.is_gate = True

        if matching.group("upx") is not None:
            mx.logv(f"'upx' is enabled for {config_name}")
            self.use_upx = True

        if matching.group("quickbuild") is not None:
            mx.logv(f"'quickbuild' is enabled for {config_name}")
            self.is_quickbuild = True

        if matching.group("gc") is not None:
            gc = matching.group("gc")[:-1]
            if gc == "g1gc":
                mx.logv(f"'g1gc' is enabled for {config_name}")
                self.gc = "G1"
            else:
                mx.abort(f"Unknown GC: {gc}")

        if matching.group("llvm") is not None:
            mx.logv(f"'llvm' mode is enabled for {config_name}")
            self.is_llvm = True

        if matching.group("pgo") is not None:
            pgo_mode = matching.group("pgo")[:-1]
            if pgo_mode == "pgo":
                mx.logv(f"'pgo' is enabled for {config_name}")
                self.pgo_instrumentation = True
            elif pgo_mode == "pgo-ctx-insens":
                mx.logv(f"'pgo-ctx-insens' is enabled for {config_name}")
                self.pgo_instrumentation = True
                self.pgo_context_sensitive = False
            else:
                mx.abort(f"Unknown pgo mode: {pgo_mode}")

        if matching.group("jdk_profiles") is not None:
            config = matching.group("jdk_profiles")[:-1]
            if config == 'jdk-profiles-collect':
                self.jdk_profiles_collect = True
                self.pgo_instrumentation = True

                def generate_profiling_package_prefixes():
                    # run the native-image-configure tool to gather the jdk package prefixes
                    graalvm_home_bin = os.path.join(mx_sdk_vm.graalvm_home(), 'bin')
                    native_image_configure_command = mx.cmd_suffix(os.path.join(graalvm_home_bin, 'native-image-configure'))
                    if not exists(native_image_configure_command):
                        mx.abort('Failed to find the native-image-configure command at {}. \nContent {}: \n\t{}'.format(native_image_configure_command, graalvm_home_bin, '\n\t'.join(os.listdir(graalvm_home_bin))))
                    tmp = tempfile.NamedTemporaryFile()
                    ret = mx.run([native_image_configure_command, 'generate-filters',
                                  '--include-packages-from-modules=java.base',
                                  '--exclude-classes=org.graalvm.**', '--exclude-classes=com.oracle.**',  # remove internal packages
                                  f'--output-file={tmp.name}'], nonZeroIsFatal=True)
                    if ret != 0:
                        mx.abort('Native image configure command failed.')

                    # format the profiling package prefixes
                    with open(tmp.name, 'r') as f:
                        prefixes = json.loads(f.read())
                        if 'rules' not in prefixes:
                            mx.abort('Native image configure command failed. Can not generate rules.')
                        rules = prefixes['rules']
                        rules = map(lambda r: r['includeClasses'][:-2], filter(lambda r: 'includeClasses' in r, rules))
                        return ','.join(rules)
                self.generate_profiling_package_prefixes = generate_profiling_package_prefixes
            elif config == 'adopted-jdk-pgo':
                self.adopted_jdk_pgo = True
            else:
                mx.abort(f'Unknown jdk profiles configuration: {config}')

        if matching.group("profile_inference") is not None:
            profile_inference_config = matching.group("profile_inference")[:-1]
            if profile_inference_config == 'profile-inference-feature-extraction':
                self.profile_inference_feature_extraction = True
                self.pgo_instrumentation = True # extract code features
            else:
                mx.abort('Unknown profile inference configuration: {}.'.format(profile_inference_config))

        if matching.group("sampler") is not None:
            config = matching.group("sampler")[:-1]
            if config == 'safepoint-sampler':
                self.safepoint_sampler = True
                self.pgo_instrumentation = True
            elif config == 'async-sampler':
                self.async_sampler = True
            else:
                mx.abort(f'Unknown type of sampler configuration: {config}')

        if matching.group("edition") is not None:
            edition = matching.group("edition")[:-1]
            mx.logv(f"GraalVM edition is set to: {edition}")
            self.graalvm_edition = edition

        if matching.group("optimization_level") is not None:
            olevel = matching.group("optimization_level")[:-1]
            mx.logv(f"GraalVM optimization level is set to: {olevel}")
            if olevel in ["O0", "O1", "O2", "O3"]:
                self.optimization_level = olevel
            else:
                mx.abort(f"Unknown configuration for optimization level: {olevel}")

        if matching.group("no_inlining_before_analysis") is not None:
            option = matching.group("no_inlining_before_analysis")[:-1]
            if option == "no-inline":
                mx.logv(f"not doing inlining before analysis for {config_name}")
                self.no_inlining_before_analysis = True
            else:
                mx.abort(f"Unknown configuration for no inlining before analysis: {option}")

        if matching.group("analysis_context_sensitivity") is not None:
            context_sensitivity = matching.group("analysis_context_sensitivity")[:-1]
            if context_sensitivity in ["insens", "allocsens"]:
                mx.logv(f"analysis context sensitivity {context_sensitivity} is enabled for {config_name}")
                self.analysis_context_sensitivity = context_sensitivity
            elif context_sensitivity in ["1obj", "2obj1h", "3obj2h", "4obj3h"]:
                mx.logv(f"analysis context sensitivity {context_sensitivity} is enabled for {config_name}")
                self.analysis_context_sensitivity = f"_{context_sensitivity}"
            else:
                mx.abort(f"Unknown analysis context sensitivity: {context_sensitivity}")


    @staticmethod
    def supported_vm_arg_prefixes():
        """
            This list is intentionally restrictive. We want to be sure that what we add is correct on the case-by-case
            basis. In the future we can convert this from a failure into a warning.
            :return: a list of args supported by native image.
        """
        return ['-D', '-Xmx', '-Xmn', '-XX:-PrintGC', '-XX:+PrintGC', '--add-opens', '--add-modules', '--add-exports',
                '--add-reads']

    _VM_OPTS_SPACE_SEPARATED_ARG = ['-mp', '-modulepath', '-limitmods', '-addmods', '-upgrademodulepath', '-m',
                                    '--module-path', '--limit-modules', '--add-modules', '--upgrade-module-path',
                                    '--module', '--module-source-path', '--add-exports', '--add-opens', '--add-reads',
                                    '--patch-module', '--boot-class-path', '--source-path', '-cp', '-classpath', '-p']

    @staticmethod
    def _split_vm_arguments(args):
        i = 0
        while i < len(args):
            arg = args[i]
            if arg == '-jar':
                return args[:i], args[i:i + 2], args[i + 2:]
            elif not arg.startswith('-'):
                return args[:i], [args[i]], args[i + 1:]
            elif arg in NativeImageVM._VM_OPTS_SPACE_SEPARATED_ARG:
                i += 2
            else:
                i += 1

        mx.abort('No executable found in args: ' + str(args))

    @staticmethod
    def extract_benchmark_arguments(args):
        i = 0
        clean_args = args[:]
        split_run = None
        while i < len(args):
            if args[i].startswith('--split-run'):
                split_run = clean_args.pop(i + 1)
                clean_args.pop(i)
            if args[i].startswith('--jvmArgsPrepend'):
                clean_args[i + 1] = ' '.join([x for x in args[i + 1].split(' ') if "-Dnative-image" not in x])
                i += 2
            else:
                i += 1
        clean_args = [x for x in clean_args if "-Dnative-image" not in x]
        vm_args, executable, image_run_args = NativeImageVM._split_vm_arguments(clean_args)

        classpath_arguments = []
        modulepath_arguments = []
        system_properties = [a for a in vm_args if a.startswith('-D')]
        image_vm_args = []
        i = 0
        while i < len(vm_args):
            vm_arg = vm_args[i]
            if vm_arg.startswith('--class-path'):
                classpath_arguments.append(vm_arg)
                i += 1
            elif vm_arg.startswith('-cp') or vm_arg.startswith('-classpath'):
                classpath_arguments += [vm_arg, vm_args[i + 1]]
                i += 2
            elif vm_arg.startswith('-p') or vm_arg.startswith('-modulepath'):
                modulepath_arguments += [vm_arg, vm_args[i + 1]]
                i += 2
            else:
                if not any(vm_arg.startswith(elem) for elem in NativeImageVM.supported_vm_arg_prefixes()):
                    mx.abort('Unsupported argument ' + vm_arg + '.' +
                             ' Currently supported argument prefixes are: ' + str(NativeImageVM.supported_vm_arg_prefixes()))
                if vm_arg in NativeImageVM._VM_OPTS_SPACE_SEPARATED_ARG:
                    image_vm_args.append(vm_args[i])
                    image_vm_args.append(vm_args[i + 1])
                    i += 2
                else:
                    image_vm_args.append(vm_arg)
                    i += 1

        return executable, classpath_arguments, modulepath_arguments, system_properties, image_vm_args, image_run_args, split_run

    def image_build_rules(self, output, benchmarks, bmSuiteArgs):
        return self.image_build_general_rules(output, benchmarks, bmSuiteArgs) + self.image_build_analysis_rules(output, benchmarks, bmSuiteArgs) \
               + self.image_build_statistics_rules(output, benchmarks, bmSuiteArgs) + self.image_build_timers_rules(output, benchmarks, bmSuiteArgs)

    def image_build_general_rules(self, output, benchmarks, bmSuiteArgs):
        class NativeImageTimeToInt(object):
            def __call__(self, *args, **kwargs):
                return int(float(args[0].replace(',', '')))

        class NativeImageHexToInt(object):
            def __call__(self, *args, **kwargs):
                return int(args[0], 16)

        return [
            mx_benchmark.StdOutRule(
                r"The executed image size for benchmark (?P<bench_suite>[a-zA-Z0-9_\-]+):(?P<benchmark>[a-zA-Z0-9_\-]+) is (?P<value>[0-9]+) B",
                {
                    "bench-suite": ("<bench_suite>", str),
                    "benchmark": ("<benchmark>", str),
                    "vm": "svm",
                    "metric.name": "binary-size",
                    "metric.value": ("<value>", int),
                    "metric.unit": "B",
                    "metric.type": "numeric",
                    "metric.score-function": "id",
                    "metric.better": "lower",
                    "metric.iteration": 0,
                }),
            mx_benchmark.StdOutRule(
                r"The (?P<type>[a-zA-Z0-9_\-]+) configuration size for benchmark (?P<bench_suite>[a-zA-Z0-9_\-]+):(?P<benchmark>[a-zA-Z0-9_\-]+) is (?P<value>[0-9]+) B",
                {
                    "bench-suite": ("<bench_suite>", str),
                    "benchmark": ("<benchmark>", str),
                    "vm": "svm",
                    "metric.name": "config-size",
                    "metric.value": ("<value>", int),
                    "metric.unit": "B",
                    "metric.type": "numeric",
                    "metric.score-function": "id",
                    "metric.better": "lower",
                    "metric.iteration": 0,
                    "metric.object": ("<type>", str)
                }),
            mx_benchmark.StdOutRule(r'^\[\S+:[0-9]+\][ ]+\[total\]:[ ]+(?P<time>[0-9,.]+?) ms', {
                "benchmark": benchmarks[0],
                "metric.name": "compile-time",
                "metric.type": "numeric",
                "metric.unit": "ms",
                "metric.value": ("<time>", NativeImageTimeToInt()),
                "metric.score-function": "id",
                "metric.better": "lower",
                "metric.iteration": 0,
                "metric.object": "total",
            }),
            mx_benchmark.StdOutRule(r'^[ ]*[0-9]+[ ]+.(?P<section>[a-zA-Z0-9._-]+?)[ ]+(?P<size>[0-9a-f]+?)[ ]+', {
                "benchmark": benchmarks[0],
                "metric.name": "binary-section-size",
                "metric.type": "numeric",
                "metric.unit": "B",
                "metric.value": ("<size>", NativeImageHexToInt()),
                "metric.score-function": "id",
                "metric.better": "lower",
                "metric.iteration": 0,
                "metric.object": ("<section>", str),
            })
        ]

    def image_build_analysis_rules(self, output, benchmarks, bmSuiteArgs):
        return [
            AnalysisReportJsonFileRule(self.config.image_build_reports_directory, self.is_gate, {
                "benchmark": benchmarks[0],
                "metric.name": "analysis-stats",
                "metric.type": "numeric",
                "metric.unit": "#",
                "metric.value": ("<total_call_edges>", int),
                "metric.score-function": "id",
                "metric.better": "lower",
                "metric.iteration": 0,
                "metric.object": "call-edges",
            }, ['total_call_edges']),
            AnalysisReportJsonFileRule(self.config.image_build_reports_directory, self.is_gate, {
                "benchmark": benchmarks[0],
                "metric.name": "analysis-stats",
                "metric.type": "numeric",
                "metric.unit": "#",
                "metric.value": ("<total_reachable_types>", int),
                "metric.score-function": "id",
                "metric.better": "lower",
                "metric.iteration": 0,
                "metric.object": "reachable-types",
            }, ['total_reachable_types']),
            AnalysisReportJsonFileRule(self.config.image_build_reports_directory, self.is_gate, {
                "benchmark": benchmarks[0],
                "metric.name": "analysis-stats",
                "metric.type": "numeric",
                "metric.unit": "#",
                "metric.value": ("<total_reachable_methods>", int),
                "metric.score-function": "id",
                "metric.better": "lower",
                "metric.iteration": 0,
                "metric.object": "reachable-methods",
            }, ['total_reachable_methods']),
            AnalysisReportJsonFileRule(self.config.image_build_reports_directory, self.is_gate, {
                "benchmark": benchmarks[0],
                "metric.name": "analysis-stats",
                "metric.type": "numeric",
                "metric.unit": "#",
                "metric.value": ("<total_reachable_fields>", int),
                "metric.score-function": "id",
                "metric.better": "lower",
                "metric.iteration": 0,
                "metric.object": "reachable-fields",
            }, ['total_reachable_fields']),
            AnalysisReportJsonFileRule(self.config.image_build_reports_directory, self.is_gate, {
                "benchmark": benchmarks[0],
                "metric.name": "analysis-stats",
                "metric.type": "numeric",
                "metric.unit": "B",
                "metric.value": ("<total_memory_bytes>", int),
                "metric.score-function": "id",
                "metric.better": "lower",
                "metric.iteration": 0,
                "metric.object": "memory"
            }, ['total_memory_bytes'])
        ]

    def image_build_statistics_rules(self, output, benchmarks, bmSuiteArgs):
        objects_list = ["total_array_store",
                        "total_assertion_error_nullary",
                        "total_assertion_error_object",
                        "total_class_cast",
                        "total_division_by_zero",
                        "total_illegal_argument_exception_argument_is_not_an_array",
                        "total_illegal_argument_exception_negative_length",
                        "total_integer_exact_overflow",
                        "total_long_exact_overflow",
                        "total_null_pointer",
                        "total_out_of_bounds"]
        metric_objects = ["total_devirtualized_invokes"]
        for obj in objects_list:
            metric_objects.append(obj + "_after_parse_canonicalization")
            metric_objects.append(obj + "_before_high_tier")
            metric_objects.append(obj + "_after_high_tier")
        rules = []
        for i in range(0, len(metric_objects)):
            rules.append(mx_benchmark.JsonFixedFileRule(self.config.image_build_stats_file, {
                "benchmark": benchmarks[0],
                "metric.name": "image-build-stats",
                "metric.type": "numeric",
                "metric.unit": "#",
                "metric.value": ("<" + metric_objects[i] + ">", int),
                "metric.score-function": "id",
                "metric.better": "lower",
                "metric.iteration": 0,
                "metric.object": metric_objects[i].replace("_", "-").replace("total-", ""),
            }, [metric_objects[i]]))
        return rules

    def image_build_timers_rules(self, output, benchmarks, bmSuiteArgs):
        class NativeImageTimeToInt(object):
            def __call__(self, *args, **kwargs):
                return int(float(args[0].replace(',', '')))

        measured_phases = ['total', 'setup', 'classlist', 'analysis', 'universe', 'compile', 'layout', 'dbginfo',
                           'image', 'write']
        rules = []
        for i in range(0, len(measured_phases)):
            phase = measured_phases[i]
            value_name = phase + "_time"
            rules.append(
                mx_benchmark.JsonFixedFileRule(self.config.image_build_stats_file, {
                    "benchmark": benchmarks[0],
                    "metric.name": "compile-time",
                    "metric.type": "numeric",
                    "metric.unit": "ms",
                    "metric.value": ("<" + value_name + ">", NativeImageTimeToInt()),
                    "metric.score-function": "id",
                    "metric.better": "lower",
                    "metric.iteration": 0,
                    "metric.object": phase,
                }, [value_name]))
            value_name = phase + "_memory"
            rules.append(
                mx_benchmark.JsonFixedFileRule(self.config.image_build_stats_file, {
                    "benchmark": benchmarks[0],
                    "metric.name": "compile-time",
                    "metric.type": "numeric",
                    "metric.unit": "B",
                    "metric.value": ("<" + value_name + ">", NativeImageTimeToInt()),
                    "metric.score-function": "id",
                    "metric.better": "lower",
                    "metric.iteration": 0,
                    "metric.object": phase + "_memory",
                }, [value_name]))
        return rules

    def rules(self, output, benchmarks, bmSuiteArgs):
        rules = super().rules(output, benchmarks, bmSuiteArgs)

        if self.stages_info.fallback_mode or self.stages_info.effective_stage == "image":
            # Only apply image build rules for the image build stages
            rules += self.image_build_rules(output, benchmarks, bmSuiteArgs)

        return rules

    @staticmethod
    def copy_bundle_output(config):
        """
        Copies all files from the bundle build into the benchmark build location.
        By default, the bundle output is produced next to the bundle file.
        """
        bundle_dir = os.path.dirname(config.bundle_path)
        bundle_name = os.path.basename(config.bundle_path)
        bundle_output = os.path.join(bundle_dir, bundle_name[:-len(".nib")] + ".output", "default")
        shutil.copytree(bundle_output, config.output_dir, dirs_exist_ok=True)
        mx.rmtree(bundle_output)

    def run_stage_agent(self):
        hotspot_vm_args = ['-ea', '-esa'] if self.is_gate and not self.config.skip_agent_assertions else []
        hotspot_vm_args += self.config.extra_jvm_args
        agentlib_options = ['native-image-agent=config-output-dir=' + str(self.config.config_dir)] + self.config.extra_agentlib_options
        hotspot_vm_args += ['-agentlib:' + ','.join(agentlib_options)]

        # Native Image has the following option enabled by default. In order to create lambda classes in the same way
        # during the agent run and image run, we need this option for the agent too.
        hotspot_vm_args += ['-Djdk.internal.lambda.disableEagerInitialization=true']

        # Jargraal is very slow with the agent, and libgraal is usually not built for Native Image benchmarks. Therefore, don't use the GraalVM compiler.
        hotspot_vm_args += ['-XX:-UseJVMCICompiler']

        # Limit parallelism because the JVMTI operations in the agent sometimes scale badly.
        if mx.cpu_count() > 8:
            hotspot_vm_args += ['-XX:ActiveProcessorCount=8']

        if self.config.image_vm_args is not None:
            hotspot_vm_args += self.config.image_vm_args

        hotspot_args = hotspot_vm_args + self.config.classpath_arguments + self.config.modulepath_arguments + self.config.system_properties + self.config.executable + self.config.extra_agent_run_args
        with self.stages.set_command(self.generate_java_command(hotspot_args)) as s:
            s.execute_command()

        path = os.path.join(self.config.config_dir, "config.zip")
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(self.config.config_dir):
                for file in files:
                    if file.endswith(".json"):
                        zipf.write(os.path.join(root, file), os.path.relpath(os.path.join(root, file), os.path.join(path, '..')))

    def run_stage_instrument_image(self, out):
        executable_name_args = ['-o', self.config.instrumentation_executable_name]
        instrument_args = ['--pgo-instrument', '-R:ProfilesDumpFile=' + self.config.profile_path]
        if self.jdk_profiles_collect:
            instrument_args += svm_experimental_options(['-H:+AOTPriorityInline', '-H:-SamplingCollect', f'-H:ProfilingPackagePrefixes={self.generate_profiling_package_prefixes()}'])

        with self.stages.set_command(self.config.base_image_build_args + executable_name_args + instrument_args) as s:
            s.execute_command()
            if self.config.bundle_path is not None:
                NativeImageVM.copy_bundle_output(self.config)
            if s.exit_code == 0:
                image_size = os.stat(os.path.join(self.config.output_dir, self.config.instrumentation_executable_name)).st_size
                out('Instrumented image size: ' + str(image_size) + ' B')

    def _ensureSamplesAreInProfile(self, profile_path):
        # If your benchmark suite fails this assertion and the suite does not expect PGO Sampling profiles (e.g. Truffle workloads)
        # Override checkSamplesInPgo in your suite and have it return False.
        if not self.bmSuite.checkSamplesInPgo():
            return
        # GR-42738 --pgo-sampling does not work with LLVM. Sampling is disabled when doing JDK profiles collection.
        if not self.is_llvm and not self.jdk_profiles_collect:
            with open(profile_path) as profile_file:
                parsed = json.load(profile_file)
                samples = parsed["samplingProfiles"]
                assert len(samples) != 0, "No sampling profiles in iprof file " + profile_path
                for sample in samples:
                    assert ":" in sample["ctx"], "Sampling profiles seem malformed in file " + profile_path
                    assert len(sample["records"]) == 1, "Sampling profiles seem to be missing records in file " + profile_path
                    assert sample["records"][0] > 0, "Sampling profiles seem to have a 0 in records in file " + profile_path

    def run_stage_instrument_run(self):
        image_run_cmd = [os.path.join(self.config.output_dir, self.config.instrumentation_executable_name)]
        image_run_cmd += self.config.extra_jvm_args
        image_run_cmd += self.config.extra_profile_run_args
        with self.stages.set_command(image_run_cmd) as s:
            s.execute_command()
            if s.exit_code == 0:
                print(f"Profile file {self.config.profile_path} sha1 is {mx.sha1OfFile(self.config.profile_path)}")
                self._ensureSamplesAreInProfile(self.config.profile_path)
            else:
                print(f"Profile file {self.config.profile_path} not dumped. Instrument run failed with exit code {s.exit_code}")

    def _print_binary_size(self, out):
        # The image size for benchmarks is tracked by printing on stdout and matching the rule.
        image_path = os.path.join(self.config.output_dir, self.config.final_image_name)
        if self.config.bundle_create_path is not None:
            image_path = os.path.join(self.config.output_dir, self.config.bundle_create_path[:-len("reports")], self.config.bundle_create_path.split(".")[0])
        image_size = os.stat(image_path).st_size
        out(f'The executed image size for benchmark {self.config.benchmark_suite_name}:{self.config.benchmark_name} is {image_size} B')

    def run_stage_image(self, out):
        executable_name_args = ['-o', self.config.final_image_name]
        pgo_args = ['--pgo=' + self.config.profile_path]
        pgo_args += svm_experimental_options(['-H:' + ('+' if self.pgo_context_sensitive else '-') + 'PGOContextSensitivityEnabled'])
        if self.adopted_jdk_pgo:
            # choose appropriate profiles
            jdk_version = mx.get_jdk().javaCompliance
            jdk_profiles = f"JDK{jdk_version}_PROFILES"
            adopted_profiles_lib = mx.library(jdk_profiles, fatalIfMissing=False)
            if adopted_profiles_lib:
                adopted_profiles_dir = adopted_profiles_lib.get_path(True)
                adopted_profile = os.path.join(adopted_profiles_dir, 'jdk_profile.iprof')
            else:
                mx.warn(f'SubstrateVM Enterprise with JDK{jdk_version} does not contain JDK profiles.')
                adopted_profile = os.path.join(mx.suite('substratevm-enterprise').mxDir, 'empty.iprof')
            jdk_profiles_args = svm_experimental_options([f'-H:AdoptedPGOEnabled={adopted_profile}'])
        else:
            jdk_profiles_args = []
        if self.profile_inference_feature_extraction:
            ml_args = svm_experimental_options(['-H:+MLGraphFeaturesExtraction', '-H:+ProfileInferenceDumpFeatures'])
            dump_file_flag = 'ProfileInferenceDumpFile'
            if dump_file_flag not in ''.join(self.config.base_image_build_args):
                mx.warn("To dump the profile inference features to a specific location, please set the '{}' flag.".format(dump_file_flag))
        else:
            ml_args = []
        final_image_command = self.config.base_image_build_args + executable_name_args + (pgo_args if self.pgo_instrumentation else []) + jdk_profiles_args + ml_args
        with self.stages.set_command(final_image_command) as s:
            s.execute_command()
            if self.config.bundle_path is not None:
                NativeImageVM.copy_bundle_output(self.config)

            if s.exit_code == 0:
                image_path = os.path.join(self.config.output_dir, self.config.final_image_name)
                if self.use_upx:
                    upx_directory = mx.library("UPX", True).get_path(True)
                    upx_path = os.path.join(upx_directory, mx.exe_suffix("upx"))
                    upx_cmd = [upx_path, image_path]
                    mx.log(f"Compressing image: {' '.join(upx_cmd)}")
                    mx.run(upx_cmd, s.stdout(True), s.stderr(True))

                self._print_binary_size(out)
                image_sections_command = "objdump -h " + image_path
                out(subprocess.check_output(image_sections_command, shell=True, universal_newlines=True))
                for config_type in ['jni', 'proxy', 'predefined-classes', 'reflect', 'resource', 'serialization']:
                    config_path = os.path.join(self.config.config_dir, config_type + '-config.json')
                    if os.path.exists(config_path):
                        config_size = os.stat(config_path).st_size
                        out('The ' + config_type + ' configuration size for benchmark ' + self.config.benchmark_suite_name + ':' + self.config.benchmark_name + ' is ' + str(config_size) + ' B')

    def run_stage_run(self, out):
        if not self.config.is_runnable:
            mx.abort(f"Benchmark {self.config.benchmark_suite_name}:{self.config.benchmark_name} is not runnable.")
        image_path = os.path.join(self.config.output_dir, self.config.final_image_name)
        with self.stages.set_command([image_path] + self.config.extra_jvm_args + self.config.image_run_args) as s:
            s.execute_command(vm=self)

    def run_java(self, args, out=None, err=None, cwd=None, nonZeroIsFatal=False):
        # This is also called with -version to gather information about the Java VM. Since this is not technically a
        # Java VM, we delegate to the superclass
        if '-version' in args:
            return super(NativeImageVM, self).run_java(args, out=out, err=err, cwd=cwd, nonZeroIsFatal=nonZeroIsFatal)

        assert self.bmSuite, "Benchmark suite was not registered."
        assert callable(getattr(self.bmSuite, "run_stage", None)), "Benchmark suite is not a NativeImageMixin."

        if not self.bmSuite.stages_info:
            def fullname(cls):
                return cls.__module__ + '.' + cls.__qualname__

            mx.abort(
                f"Invalid Native Image benchmark setup for {fullname(self.bmSuite.__class__)}.\n"
                f"Please see {fullname(NativeImageBenchmarkMixin)} for more information.",
            )

        self.stages_info: StagesInfo = self.bmSuite.stages_info
        assert not self.stages_info.failed, "In case of a failed benchmark, no further calls into the VM should be made"

        # never fatal, we handle it ourselves
        self.config = NativeImageBenchmarkConfig(self, self.bmSuite, args)
        self.stages = NativeImageStages(self.stages_info, self.config, out, err, self.is_gate, True if self.is_gate else nonZeroIsFatal, os.path.abspath(cwd if cwd else os.getcwd()))

        os.makedirs(self.config.output_dir, exist_ok=True)
        os.makedirs(self.config.config_dir, exist_ok=True)

        if self.stages_info.fallback_mode:
            # In fallback mode, we have to run all requested stages in the same `run_java` invocation.
            # We simply emulate the dispatching of the individual stages as in `NativeImageBenchmarkMixin.intercept_run`
            for stage in self.stages_info.effective_stages:
                self.stages_info.change_stage(stage)
                self.run_single_stage(out)
        else:
            self.run_single_stage(out)

        if self.stages_info.failed:
            mx.abort('Exiting the benchmark due to the failure.')

    def run_single_stage(self, out):
        if self.stages_info.skip_current_stage:
            self.stages.bench_out(f"{mx_sdk_benchmark.STAGE_SKIPPED_PREFIX} {self.stages_info.requested_stage}")
            return

        stage_to_run = self.stages_info.effective_stage
        if stage_to_run == "agent":
            self.run_stage_agent()
        elif stage_to_run == "instrument-image":
            self.run_stage_instrument_image(out)
        elif stage_to_run == "instrument-run":
            self.run_stage_instrument_run()
        elif stage_to_run == "image":
            self.run_stage_image(out)
        elif stage_to_run == "run":
            self.run_stage_run(out)
        else:
            raise ValueError(f"Unknown stage {stage_to_run}")


class AnalysisReportJsonFileRule(mx_benchmark.JsonBaseRule):
    """Rule that looks for JSON file names in the output of the benchmark and looks up the files in the report directory"""

    def __init__(self, report_directory, is_diagnostics_mode, replacement, keys):
        super(AnalysisReportJsonFileRule, self).__init__(replacement, keys)
        self.pattern = r'^# Printing analysis results stats to: (?P<path>\S+?)$'
        self.match_name = 'path'
        self.is_diagnostics_mode = is_diagnostics_mode
        self.report_directory = report_directory

    def getJsonFiles(self, text):
        json_files = (m.groupdict()[self.match_name] for m in re.finditer(self.pattern, text, re.MULTILINE))
        found_json_files = []
        for json_file_path in json_files:
            json_file_name = os.path.basename(json_file_path)
            base_search_dir = self.report_directory
            if self.is_diagnostics_mode:
                base_search_dir = os.path.join(base_search_dir, os.path.basename(os.path.dirname(json_file_path)))
            expected_json_file_path = os.path.join(base_search_dir, json_file_name)
            if exists(expected_json_file_path):
                found_json_files.append(expected_json_file_path)
            else:
                assert False, f"Matched file does not exist at {expected_json_file_path}. The file was matched from standard output, with the original path: {json_file_path}"
        return found_json_files


class AgentScriptJsBenchmarkSuite(mx_benchmark.VmBenchmarkSuite, mx_benchmark.AveragingBenchmarkMixin):
    def __init__(self):
        super(AgentScriptJsBenchmarkSuite, self).__init__()
        self._benchmarks = {
            'plain' : [],
            'triple' : ['--insight=sieve-filter1.js'],
            'single' : ['--insight=sieve-filter2.js'],
            'iterate' : ['--insight=sieve-filter3.js'],
        }

    def group(self):
        return 'Graal'

    def subgroup(self):
        return 'graal-js'

    def name(self):
        return 'agentscript'

    def version(self):
        return '0.1.0'

    def benchmarkList(self, bmSuiteArgs):
        return self._benchmarks.keys()

    def failurePatterns(self):
        return [
            re.compile(r'error:'),
            re.compile(r'internal error:'),
            re.compile(r'Error in'),
            re.compile(r'\tat '),
            re.compile(r'Defaulting the .*\. Consider using '),
            re.compile(r'java.lang.OutOfMemoryError'),
        ]

    def successPatterns(self):
        return [
            re.compile(r'Hundred thousand prime numbers in [0-9]+ ms', re.MULTILINE),
        ]

    def rules(self, out, benchmarks, bmSuiteArgs):
        assert len(benchmarks) == 1
        return [
            mx_benchmark.StdOutRule(r'^Hundred thousand prime numbers in (?P<time>[0-9]+) ms$', {
                "bench-suite": self.name(),
                "benchmark": (benchmarks[0], str),
                "metric.name": "warmup",
                "metric.type": "numeric",
                "metric.unit": "ms",
                "metric.value": ("<time>", int),
                "metric.score-function": "id",
                "metric.better": "lower",
                "metric.iteration": ("$iteration", int),
            })
        ]

    def createCommandLineArgs(self, benchmarks, bmSuiteArgs):
        return self.vmArgs(bmSuiteArgs) + super(AgentScriptJsBenchmarkSuite, self).createCommandLineArgs(benchmarks, bmSuiteArgs)

    def workingDirectory(self, benchmarks, bmSuiteArgs):
        return os.path.join(_suite.dir, 'benchmarks', 'agentscript')

    def createVmCommandLineArgs(self, benchmarks, runArgs):
        if not benchmarks:
            raise mx.abort(f"Benchmark suite '{self.name()}' cannot run multiple benchmarks in the same VM process")
        if len(benchmarks) != 1:
            raise mx.abort(f"Benchmark suite '{self.name()}' can run only one benchmark at a time")
        return self._benchmarks[benchmarks[0]] + ['-e', 'count=50'] + runArgs + ['sieve.js']

    def get_vm_registry(self):
        return mx_benchmark.js_vm_registry

    def run(self, benchmarks, bmSuiteArgs) -> DataPoints:
        results = super(AgentScriptJsBenchmarkSuite, self).run(benchmarks, bmSuiteArgs)
        self.addAverageAcrossLatestResults(results)
        return results


class ExcludeWarmupRule(mx_benchmark.StdOutRule):
    """Rule that behaves as the StdOutRule, but skips input until a certain pattern."""

    def __init__(self, *args, **kwargs):
        self.startPattern = re.compile(kwargs.pop('startPattern'))
        super(ExcludeWarmupRule, self).__init__(*args, **kwargs)

    def parse(self, text) -> Iterable[DataPoint]:
        m = self.startPattern.search(text)
        if m:
            return super(ExcludeWarmupRule, self).parse(text[m.end()+1:])
        else:
            return []


class PolyBenchBenchmarkSuite(mx_benchmark.VmBenchmarkSuite):
    def __init__(self):
        super(PolyBenchBenchmarkSuite, self).__init__()
        self._extensions = [".js", ".rb", ".wasm", ".bc", ".py", ".jar", ".pmh"]

    def _get_benchmark_root(self):
        if not hasattr(self, '_benchmark_root'):
            dist_name = "POLYBENCH_BENCHMARKS"
            distribution = mx.distribution(dist_name)
            _root = distribution.get_output()
            if not os.path.exists(_root):
                msg = f"The distribution {dist_name} does not exist: {_root}{os.linesep}"
                msg += f"This might be solved by running: mx build --dependencies={dist_name}"
                mx.abort(msg)
            self._benchmark_root = _root
        return self._benchmark_root

    def group(self):
        return "Graal"

    def subgroup(self):
        return "truffle"

    def name(self):
        return "polybench"

    def version(self):
        return "0.1.0"

    def benchmarkList(self, bmSuiteArgs):
        if not hasattr(self, "_benchmarks"):
            self._benchmarks = []
            graal_test = mx.distribution('GRAAL_TEST', fatalIfMissing=False)
            polybench_ee = mx.distribution('POLYBENCH_EE', fatalIfMissing=False)
            if graal_test and polybench_ee and mx.get_env('ENABLE_POLYBENCH_HPC') == 'yes':
                # If the GRAAL_TEST and POLYBENCH_EE (for instructions metric) distributions
                # are present, the CompileTheWorld benchmark is available.
                self._benchmarks = ['CompileTheWorld']
            for group in ["interpreter", "compiler", "warmup", "nfi"]:
                dir_path = os.path.join(self._get_benchmark_root(), group)
                for f in os.listdir(dir_path):
                    f_path = os.path.join(dir_path, f)
                    if os.path.isfile(f_path) and os.path.splitext(f_path)[1] in self._extensions:
                        self._benchmarks.append(os.path.join(group, f))
        return self._benchmarks

    def workingDirectory(self, benchmarks, bmSuiteArgs):
        return self._get_benchmark_root()

    def createCommandLineArgs(self, benchmarks, bmSuiteArgs):
        if benchmarks is None or len(benchmarks) != 1:
            mx.abort("Must specify one benchmark at a time.")
        vmArgs = self.vmArgs(bmSuiteArgs)
        benchmark = benchmarks[0]
        if benchmark == 'CompileTheWorld':
            # Run CompileTheWorld as a polybench benchmark, using instruction counting to get a stable metric.
            # The CompileTheWorld class has been reorganized to have separate "prepare" and
            # "compile" steps such that only the latter is measured by polybench.
            # PAPI instruction counters are thread-local so CTW is run on the same thread as
            # the polybench harness (i.e., CompileTheWorld.MultiThreaded=false).
            import mx_compiler
            res = mx_compiler._ctw_jvmci_export_args(arg_prefix='--vm.-') + [
                   '--ctw',
                   '--vm.cp=' + mx.distribution('GRAAL_TEST').path,
                   '--vm.DCompileTheWorld.MaxCompiles=10000',
                   '--vm.DCompileTheWorld.Classpath=' + mx.library('DACAPO_MR1_BACH').get_path(resolve=True),
                   '--vm.DCompileTheWorld.Verbose=false',
                   '--vm.DCompileTheWorld.MultiThreaded=false',
                   '--vm.Djdk.libgraal.ShowConfiguration=info',
                   '--metric=instructions',
                   '-w', '1',
                   '-i', '5'] + vmArgs
        else:
            benchmark_path = os.path.join(self._get_benchmark_root(), benchmark)
            res = ["--path=" + benchmark_path] + vmArgs
        return res

    def get_vm_registry(self):
        return _polybench_vm_registry

    def rules(self, output, benchmarks, bmSuiteArgs):
        metric_name = self._get_metric_name(output)
        rules = []
        if metric_name == "time":
            # Special case for metric "time": Instead of reporting the aggregate numbers,
            # report individual iterations. Two metrics will be reported:
            # - "warmup" includes all iterations (warmup and run)
            # - "time" includes only the "run" iterations
            rules += [
                mx_benchmark.StdOutRule(r"\[(?P<name>.*)\] iteration ([0-9]*): (?P<value>.*) (?P<unit>.*)", {
                    "benchmark": ("<name>", str),
                    "metric.better": "lower",
                    "metric.name": "warmup",
                    "metric.unit": ("<unit>", str),
                    "metric.value": ("<value>", float),
                    "metric.type": "numeric",
                    "metric.score-function": "id",
                    "metric.iteration": ("$iteration", int),
                }),
                ExcludeWarmupRule(r"\[(?P<name>.*)\] iteration (?P<iteration>[0-9]*): (?P<value>.*) (?P<unit>.*)", {
                    "benchmark": ("<name>", str),
                    "metric.better": "lower",
                    "metric.name": "time",
                    "metric.unit": ("<unit>", str),
                    "metric.value": ("<value>", float),
                    "metric.type": "numeric",
                    "metric.score-function": "id",
                    "metric.iteration": ("<iteration>", int),
                }, startPattern=r"::: Running :::")
            ]
        elif metric_name in ("allocated-memory", "metaspace-memory", "application-memory"):
            rules += [
                ExcludeWarmupRule(r"\[(?P<name>.*)\] iteration (?P<iteration>[0-9]*): (?P<value>.*) (?P<unit>.*)", {
                    "benchmark": ("<name>", str),
                    "metric.better": "lower",
                    "metric.name": metric_name,
                    "metric.unit": ("<unit>", str),
                    "metric.value": ("<value>", float),
                    "metric.type": "numeric",
                    "metric.score-function": "id",
                    "metric.iteration": ("<iteration>", int),
                }, startPattern=r"::: Running :::")
            ]
        else:
            rules += [
                mx_benchmark.StdOutRule(r"\[(?P<name>.*)\] after run: (?P<value>.*) (?P<unit>.*)", {
                    "benchmark": ("<name>", str),
                    "metric.better": "lower",
                    "metric.name": metric_name,
                    "metric.unit": ("<unit>", str),
                    "metric.value": ("<value>", float),
                    "metric.type": "numeric",
                    "metric.score-function": "id",
                    "metric.iteration": 0,
                })
            ]
        rules += [
            mx_benchmark.StdOutRule(r"### load time \((?P<unit>.*)\): (?P<delta>[0-9]+)", {
                "benchmark": benchmarks[0],
                "metric.name": "context-eval-time",
                "metric.value": ("<delta>", float),
                "metric.unit": ("<unit>", str),
                "metric.type": "numeric",
                "metric.score-function": "id",
                "metric.better": "lower",
                "metric.iteration": 0
            }),
            mx_benchmark.StdOutRule(r"### init time \((?P<unit>.*)\): (?P<delta>[0-9]+)", {
                "benchmark": benchmarks[0],
                "metric.name": "context-init-time",
                "metric.value": ("<delta>", float),
                "metric.unit": ("<unit>", str),
                "metric.type": "numeric",
                "metric.score-function": "id",
                "metric.better": "lower",
                "metric.iteration": 0
            })
        ]
        return rules

    def _get_metric_name(self, bench_output):
        match = re.search(r"metric class:\s*(?P<metric_class_name>\w+)Metric", bench_output)
        if match is None:
            match = re.search(r"metric class:\s*(?P<metric_class_name>\w+)", bench_output)

        metric_class_name = match.group("metric_class_name")
        metric_class_name = re.sub(r'(?<!^)(?=[A-Z])', '-', metric_class_name).lower()

        if metric_class_name in POLYBENCH_METRIC_MAPPING:
            return POLYBENCH_METRIC_MAPPING[metric_class_name]
        else:
            return metric_class_name


class FileSizeBenchmarkSuite(mx_benchmark.VmBenchmarkSuite):
    SZ_MSG_PATTERN = "== binary size == {} is {} bytes, path = {}\n"
    SZ_RGX_PATTERN = r"== binary size == (?P<image_name>[a-zA-Z0-9_\-\.:]+) is (?P<value>[0-9]+) bytes, path = (?P<path>.*)"


    def group(self):
        return "Graal"

    def subgroup(self):
        return "truffle"

    def name(self):
        return "file-size"

    def version(self):
        return "0.0.1"

    def benchmarkList(self, bmSuiteArgs):
        return ["default"]

    def get_vm_registry(self):
        return _polybench_vm_registry

    def runAndReturnStdOut(self, benchmarks, bmSuiteArgs):
        vm = self.get_vm_registry().get_vm_from_suite_args(bmSuiteArgs)
        vm.extract_vm_info(self.vmArgs(bmSuiteArgs))
        host_vm = None
        if isinstance(vm, mx_benchmark.GuestVm):
            host_vm = vm.host_vm()
            assert host_vm
        name = 'graalvm-ee' if mx_sdk_vm_impl.has_component('svmee', stage1=True) else 'graalvm-ce'
        dims = {
            # the vm and host-vm fields are hardcoded to one of the accepted names of the field
            "vm": name,
            "host-vm": name,
            "host-vm-config": self.host_vm_config_name(host_vm, vm),
            "guest-vm": name if host_vm else "none",
            "guest-vm-config": self.guest_vm_config_name(host_vm, vm),
        }

        out = ""
        output_root = mx_sdk_vm_impl.get_final_graalvm_distribution().get_output_root()

        def get_size_message(image_name, image_location):
            return FileSizeBenchmarkSuite.SZ_MSG_PATTERN.format(image_name, getsize(os.path.join(output_root, image_location)), image_location, output_root)

        for location in mx_sdk_vm_impl.get_all_native_image_locations(include_libraries=True, include_launchers=False, abs_path=False):
            lib_name = 'lib:' + mx_sdk_vm_impl.remove_lib_prefix_suffix(basename(location))
            out += get_size_message(lib_name, location)
        for location in mx_sdk_vm_impl.get_all_native_image_locations(include_libraries=False, include_launchers=True, abs_path=False):
            launcher_name = mx_sdk_vm_impl.remove_exe_suffix(basename(location))
            out += get_size_message(launcher_name, location)
        if out:
            mx.log(out, end='')
        return 0, out, dims

    def rules(self, output, benchmarks, bmSuiteArgs):
        return [
            mx_benchmark.StdOutRule(
                FileSizeBenchmarkSuite.SZ_RGX_PATTERN,
                {
                    "bench-suite": self.name(),
                    "benchmark": ("<image_name>", str),
                    "benchmark-configuration": ("<path>", str),
                    "vm": "svm",
                    "metric.name": "binary-size",
                    "metric.value": ("<value>", int),
                    "metric.unit": "B",
                    "metric.type": "numeric",
                    "metric.score-function": "id",
                    "metric.better": "lower",
                    "metric.iteration": 0,
                })
        ]


class PolyBenchVm(GraalVm):
    def __init__(self, name, config_name, extra_java_args, extra_launcher_args):
        super(PolyBenchVm, self).__init__(name, config_name, extra_java_args, extra_launcher_args)
        if self.debug_args:
            # The `arg[1:]` is to strip the first '-' from the args since it's
            # re-added by the subsequent processing of `--vm`
            self.debug_args = [f'--vm.{arg[1:]}' for arg in self.debug_args]

    def run(self, cwd, args):
        return self.run_launcher('polybench', args, cwd)

def polybenchmark_rules(benchmark, metric_name, mode):
    rules = []
    if metric_name == "time":
        # Special case for metric "time": Instead of reporting the aggregate numbers,
        # report individual iterations. Two metrics will be reported:
        # - "warmup" includes all iterations (warmup and run)
        # - "time" includes only the "run" iterations
        rules += [
            mx_benchmark.StdOutRule(r"\[(?P<name>.*)\] iteration ([0-9]*): (?P<value>.*) (?P<unit>.*)", {
                "benchmark": benchmark, #("<name>", str),
                "metric.better": "lower",
                "metric.name": "warmup",
                "metric.unit": ("<unit>", str),
                "metric.value": ("<value>", float),
                "metric.type": "numeric",
                "metric.score-function": "id",
                "metric.iteration": ("$iteration", int),
                "engine.config": mode,
            }),
            ExcludeWarmupRule(r"\[(?P<name>.*)\] iteration (?P<iteration>[0-9]*): (?P<value>.*) (?P<unit>.*)", {
                "benchmark": benchmark, #("<name>", str),
                "metric.better": "lower",
                "metric.name": "time",
                "metric.unit": ("<unit>", str),
                "metric.value": ("<value>", float),
                "metric.type": "numeric",
                "metric.score-function": "id",
                "metric.iteration": ("<iteration>", int),
                "engine.config": mode,
            }, startPattern=r"::: Running :::"),
            mx_benchmark.StdOutRule(r"### load time \((?P<unit>.*)\): (?P<delta>[0-9]+)", {
                "benchmark": benchmark,
                "metric.name": "context-eval-time",
                "metric.value": ("<delta>", float),
                "metric.unit": ("<unit>", str),
                "metric.type": "numeric",
                "metric.score-function": "id",
                "metric.better": "lower",
                "metric.iteration": 0,
                "engine.config": mode
            }),
            mx_benchmark.StdOutRule(r"### init time \((?P<unit>.*)\): (?P<delta>[0-9]+)", {
                "benchmark": benchmark,
                "metric.name": "context-init-time",
                "metric.value": ("<delta>", float),
                "metric.unit": ("<unit>", str),
                "metric.type": "numeric",
                "metric.score-function": "id",
                "metric.better": "lower",
                "metric.iteration": 0,
                "engine.config": mode,
            }),
        ]
    elif metric_name in ("allocated-memory", "metaspace-memory", "application-memory", "instructions"):
        rules += [
            ExcludeWarmupRule(r"\[(?P<name>.*)\] iteration (?P<iteration>[0-9]*): (?P<value>.*) (?P<unit>.*)", {
                "benchmark": benchmark, #("<name>", str),
                "metric.better": "lower",
                "metric.name": metric_name,
                "metric.unit": ("<unit>", str),
                "metric.value": ("<value>", float),
                "metric.type": "numeric",
                "metric.score-function": "id",
                "metric.iteration": ("<iteration>", int),
                "engine.config": mode,
            }, startPattern=r"::: Running :::")
        ]
    elif metric_name in ("compile-time", "pe-time"):
        rules += [
            mx_benchmark.StdOutRule(r"\[(?P<name>.*)\] after run: (?P<value>.*) (?P<unit>.*)", {
                "benchmark": benchmark, #("<name>", str),
                "metric.better": "lower",
                "metric.name": metric_name,
                "metric.unit": ("<unit>", str),
                "metric.value": ("<value>", float),
                "metric.type": "numeric",
                "metric.score-function": "id",
                "metric.iteration": 0,
                "engine.config": mode,
            }),
        ]
    return rules

mx_benchmark.add_bm_suite(AgentScriptJsBenchmarkSuite())
mx_benchmark.add_bm_suite(PolyBenchBenchmarkSuite())
mx_benchmark.add_bm_suite(FileSizeBenchmarkSuite())


def register_graalvm_vms():
    default_host_vm_name = mx_sdk_vm_impl.graalvm_dist_name().lower().replace('_', '-')
    short_host_vm_name = re.sub('-java[0-9]+$', '', default_host_vm_name)
    host_vm_names = [default_host_vm_name] + ([short_host_vm_name] if short_host_vm_name != default_host_vm_name else [])
    for host_vm_name in host_vm_names:
        for config_name, java_args, launcher_args, priority in mx_sdk_vm.get_graalvm_hostvm_configs():
            if config_name.startswith("jvm"):
                # needed for NFI CLinker benchmarks
                launcher_args += ['--vm.-enable-preview']
            mx_benchmark.java_vm_registry.add_vm(GraalVm(host_vm_name, config_name, java_args, launcher_args), _suite, priority)
            for mode, mode_options in _polybench_modes:
                _polybench_vm_registry.add_vm(PolyBenchVm(host_vm_name, config_name + "-" + mode, [], mode_options + launcher_args))
        if _suite.get_import("polybenchmarks") is not None:
            import mx_polybenchmarks_benchmark
            mx_polybenchmarks_benchmark.polybenchmark_vm_registry.add_vm(PolyBenchVm(host_vm_name, "jvm", [], ["--jvm"]))
            mx_polybenchmarks_benchmark.polybenchmark_vm_registry.add_vm(PolyBenchVm(host_vm_name, "native", [], ["--native"]))
            mx_polybenchmarks_benchmark.rules = polybenchmark_rules

    optimization_levels = ['O0', 'O1', 'O2', 'O3']

    # Inlining before analysis is done by default
    analysis_context_sensitivity = ['insens', 'allocsens', '1obj', '2obj1h', '3obj2h', '4obj3h']
    analysis_context_sensitivity_no_inline = [f"{analysis_component}-no-inline" for analysis_component in analysis_context_sensitivity]

    for short_name, config_suffix in [('niee', 'ee'), ('ni', 'ce')]:
        if any(component.short_name == short_name for component in mx_sdk_vm_impl.registered_graalvm_components(stage1=False)):
            for main_config in ['default', 'gate', 'llvm', 'native-architecture'] + analysis_context_sensitivity + analysis_context_sensitivity_no_inline:
                final_config_name = f'{main_config}-{config_suffix}'
                mx_benchmark.add_java_vm(NativeImageVM('native-image', final_config_name), _suite, 10)
                # ' '  force the empty O<> configs as well
            for main_config in ['llvm', 'native-architecture', 'g1gc', 'native-architecture-g1gc', ''] + analysis_context_sensitivity + analysis_context_sensitivity_no_inline:
                for optimization_level in optimization_levels:
                    if len(main_config) > 0:
                        final_config_name = f'{main_config}-{optimization_level}-{config_suffix}'
                    else:
                        final_config_name = f'{optimization_level}-{config_suffix}'
                    mx_benchmark.add_java_vm(NativeImageVM('native-image', final_config_name), _suite, 10)


    # Adding JAVA_HOME VMs to be able to run benchmarks on GraalVM binaries without the need of building it first
    for java_home_config in ['default', 'pgo', 'g1gc', 'g1gc-pgo', 'upx', 'upx-g1gc', 'quickbuild', 'quickbuild-g1gc']:
        mx_benchmark.add_java_vm(NativeImageVM('native-image-java-home', java_home_config), _suite, 5)


    # Add VMs for libgraal
    if mx.suite('substratevm', fatalIfMissing=False) is not None:
        import mx_substratevm
        # Use `name` rather than `short_name` since the code that follows
        # should not be executed when "LibGraal Enterprise" is registered
        if mx_sdk_vm_impl.has_component(mx_substratevm.libgraal.name):
            libgraal_location = mx_sdk_vm_impl.get_native_image_locations(mx_substratevm.libgraal.name, 'jvmcicompiler')
            if libgraal_location is not None:
                import mx_graal_benchmark
                mx_graal_benchmark.build_jvmci_vm_variants('server', 'graal-core-libgraal',
                                                           ['-server', '-XX:+EnableJVMCI', '-Djdk.graal.CompilerConfiguration=community', '-Djvmci.Compiler=graal', '-XX:+UseJVMCINativeLibrary', '-XX:JVMCILibPath=' + dirname(libgraal_location)],
                                                           mx_graal_benchmark._graal_variants, suite=_suite, priority=15, hosted=False)
