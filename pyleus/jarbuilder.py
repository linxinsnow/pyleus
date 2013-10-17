#!/usr/bin/python
"""Command-line tool for building a standalone, self-contained Pyleus topology
JAR ready to be submitted to a Storm cluster. If an optional requirements.txt
is provided, Pyleus will use virtualenv to collect and provide Python
dependencies to the running topology.

Args:
    TOPOLOGY_DIRECTORY - the directory where all the topology source files,
        the YAML file describing the topology (pyleus_topology.yaml) and the
        optional requirements.txt are found.

The script will attempt to ensure the contents of TOPOLOGY_DIRECTORY are in
order, that nothing will be improperly overwritten and that mandatory files are
present: pyleus_topology.yaml is always required and requirements.txt must
exist if --use-virtualenv is explicitly stated.

The output JAR is built from a common base JAR included in the pyleus package
by default, and will be named <TOPOLOGY_DIRECTORY>.jar.

NOTE: The names used for the YAML file and for the virtualenv CANNOT be changed
without modifying the Java code accordingly.
"""

import optparse
import glob
import re
import tempfile
import os
import shutil
import subprocess
import sys
import zipfile


BASE_JAR_PATH = "minimal.jar"
RESOURCES_PATH = "resources/"
YAML_FILENAME = "pyleus_topology.yaml"
REQUIREMENTS_FILENAME = "requirements.txt"
VIRTUALENV = "pyleus_venv"

PROG = os.path.basename(sys.argv[0])
PYLEUS_ERROR_FMT = "{0}: error: {1}"


class PyleusError(Exception):
    """Base class for pyleus specific exceptions"""
    def __str__(self):
        return "[{0}] {1}".format(type(self).__name__,
                ", ".join(str(i) for i in self.args))


class JarError(PyleusError): pass
class TopologyError(PyleusError): pass
class InvalidTopologyError(TopologyError): pass
class DependenciesError(TopologyError): pass


def _open_jar(base_jar):
    """Open the base jar file."""
    if not os.path.exists(base_jar):
        raise JarError("Base jar not found")

    if not zipfile.is_zipfile(base_jar):
        raise JarError("Base jar is not a jar file")

    zip_file = zipfile.ZipFile(base_jar, "r")

    return zip_file


def _validate_dir(topology_dir):
    """Ensure that the directory exists and is a directory"""
    if not os.path.exists(topology_dir):
        raise TopologyError("Topology directory not found: {0}".format(
            topology_dir))

    if not os.path.isdir(topology_dir):
        raise TopologyError("Topology directory is not a directory: {0}".format(
            topology_dir))


def _validate_yaml(yaml):
    """Ensure that TOPOLOGY_YAML exists inside the directory"""
    if not os.path.isfile(yaml):
        raise InvalidTopologyError("Topology YAML not found: {0}".format(yaml))


def _validate_req(req):
    """Ensure that requirements.txt exists inside the directory"""
    if not os.path.isfile(req):
        raise InvalidTopologyError("{0} file not found".format(
            REQUIREMENTS_FILENAME))


def _validate_venv(topology_dir, venv):
    """Ensure that VIRTUALENV does not exist inside the directory"""
    if os.path.exists(venv):
        raise InvalidTopologyError("Topology directory must not contain a "
            "file named {0}".format(venv))


def _validate_topology(topology_dir, yaml, req, venv,  opts):
    """Validate topology_dir to ensure that:

        - it exists and is a directory
        - TOPOLOGY_YAML exists inside
        - requirements.txt exists if --use-virtualenv was explicitly stated
        - nothing will be overwritten

    SIDE EFFECT: opts.use_virtualenv is set to False or True
    """
    _validate_dir(topology_dir)

    _validate_yaml(yaml)

    # if use_virtualenv is undefined (None), it will be assigned on the base of
    # the requirements.txt file
    if opts.use_virtualenv is None:
        opts.use_virtualenv = False if not os.path.isfile(req) else True

    if opts.use_virtualenv:
        _validate_req(req)
        _validate_venv(topology_dir, venv)


def _virtualenv_pip_install(tmp_dir, req, **kwargs):
    """Create a virtualenv with the specified options and run `pip install -r
    requirements.txt`.

    Options:
        system-site-packages - creating the virtualenv with this flag,
        pip will not download and install in the virtualenv all the
        dependencies already installed system-wide.
        index-url - allow to specify the URL of the Python Package Index.
        pip-log - a verbose log generated by pip install
    """
    virtualenv_cmd = ["virtualenv", VIRTUALENV]

    if kwargs.get("system") is True:
        virtualenv_cmd.append("--system-site-packages")

    pip_cmd = [os.path.join(VIRTUALENV, "bin", "pip"), "install", "-r", req]

    if kwargs.get("index_url") is not None:
        pip_cmd += ["-i", kwargs["index_url"]]

    if kwargs.get("pip_log") is not None:
        pip_cmd += ["--log", kwargs["pip_log"]]

    out_stream = None
    if kwargs.get("verbose") is False:
        out_stream = open(os.devnull, "w")

    ret_code = subprocess.call(virtualenv_cmd, cwd=tmp_dir, stdout=out_stream,
        stderr=subprocess.STDOUT)
    if ret_code != 0:
        raise DependenciesError("Failed to install dependencies for this "
            "topology. Failed to create virtualenv.")

    ret_code = subprocess.call(pip_cmd, cwd=tmp_dir, stdout=out_stream,
        stderr=subprocess.STDOUT)
    if ret_code != 0:
        raise DependenciesError("Failed to install dependencies for this "
            "topology. Run with --verbose for detailed info.")


def _exclude_content(src, exclude_req):
    """Remove from the content list all paths matching the patterns
    in the exclude list.
    Filtering is applied only at the top level of the directory.
    """
    content = set(glob.glob(os.path.join(src, "*")))
    yaml = os.path.join(src, YAML_FILENAME)
    content -= set([yaml])
    if exclude_req:
        req = os.path.join(src, REQUIREMENTS_FILENAME)
        content -= set([req])
    return content


def _copy_dir_content(src, dst, exclude_req):
    """Copy the content of a directory excluding the paths
    matching the patterns in the exclude list.

    This functions is used instead of shutil.copytree() because
    the latter always creates a top level directory, while only
    the content need to be copied in this case.
    """
    content = _exclude_content(src, exclude_req)

    for t in content:
        if os.path.isdir(t):
            shutil.copytree(t, os.path.join(dst, os.path.basename(t)), symlinks=True)
        else:
            shutil.copy2(t, dst)


def _zip_dir(src, arc):
    """Build a zip archive from the specified src.

    NOTE: If the archive already exists, files will be simply
    added to it, but the original archive will not be replaced.
    At the current state, this script enforce the creation of
    a brand new zip archive each time is run, otehrwise it will
    raise an exception.
    """
    src_re = re.compile(src + "/*")
    for root, dirs, files in os.walk(src):
        # hack for copying everithing but the top directory
        prefix = re.sub(src_re, "", root)
        for f in files:
            # zipfile creates directories if missing
            arc.write(os.path.join(root, f), os.path.join(prefix, f),
                    zipfile.ZIP_DEFLATED)


def _pack_jar(tmp_dir, output_jar):
    """Build a jar from the temporary directory."""
    if os.path.exists(output_jar):
        raise JarError("Output jar already exists: {0}".format(output_jar))

    zf = zipfile.ZipFile(output_jar, "w")
    try:
        _zip_dir(tmp_dir, zf)
    finally:
        zf.close()


def _inject(topology_dir, base_jar, output_jar, zip_file, tmp_dir, options):
    """Coordinate the creation of the the topology JAR:

        - Validate the topology
        - Extract the base JAR into a temporary directory
        - Copy all source files into the directory
        - If using virtualenv, create it and install dependencies
        - Re-pack the temporary directory into the final JAR
    """
    yaml = os.path.join(topology_dir, YAML_FILENAME)
    req = os.path.join(topology_dir, REQUIREMENTS_FILENAME)
    venv = os.path.join(topology_dir, VIRTUALENV)
    # Validate topolgy and return requirements and yaml file path
    _validate_topology(topology_dir, yaml, req, venv,  options)

    # Extract pyleus base jar content in a tmp dir
    zip_file.extractall(tmp_dir)

    # Copy yaml into its directory
    shutil.copy2(yaml, os.path.join(tmp_dir, RESOURCES_PATH))

    # Add the topology directory skipping yaml and requirements
    _copy_dir_content(topology_dir, os.path.join(tmp_dir, RESOURCES_PATH),
            exclude_req=not options.use_virtualenv)

    # Virtualenv + pip install used to install dependencies listed in
    # requirements.txt
    if options.use_virtualenv:
        _virtualenv_pip_install(tmp_dir=os.path.join(tmp_dir, RESOURCES_PATH),
                req=req,
                system=options.system,
                index_url=options.index_url,
                pip_log=options.pip_log,
                verbose=options.verbose)

    # Pack the tmp directory into a jar
    _pack_jar(tmp_dir, output_jar)


def _build_output_path(output_arg, topology_dir):
    """Return the absolute path of the output jar file.

    Default basename:
        TOPOLOGY_DIRECTORY.jar
    """
    if output_arg is not None:
        return os.path.abspath(output_arg)
    else:
        return os.path.abspath(os.path.basename(topology_dir) + ".jar")


def main():
    """Parse command-line arguments and invoke _inject()"""
    parser = optparse.OptionParser(
            usage="usage: %prog [options] TOPOLOGY_DIRECTORY",
            description="Build up a storm jar from a topology source directory")
    parser.add_option("-b", "--base", dest="base_jar", default=BASE_JAR_PATH,
            help="pyleus base jar file path")
    parser.add_option("-o", "--out", dest="output_jar",
            help="path of the jar file that will contain"
            " all the dependencies and the resources")
    parser.add_option("--use-virtualenv", dest="use_virtualenv",
            default=None, action="store_true",
            help="use virtualenv and pip install for dependencies."
            " Your TOPOLOGY_DIRECTORY must contain a file named {0}"
            .format(REQUIREMENTS_FILENAME))
    parser.add_option("--no-use-virtualenv",
            dest="use_virtualenv", action="store_false",
            help="do not use virtualenv and pip for dependencies")
    parser.add_option("-i", "--index-url", dest="index_url",
            help="base URL of Python Package Index used by pip"
            " (default https://pypi.python.org/simple/)")
    parser.add_option("-s", "--system-packages", dest="system",
            default=False, action="store_true",
            help="do not install packages already present in your system")
    parser.add_option("--log", dest="pip_log", help="log location for pip")
    parser.add_option("-v", "--verbose", dest="verbose",
            default=False, action="store_true",
            help="verbose")
    options, args = parser.parse_args()

    if len(args) != 1:
        parser.error("incorrect number of arguments")

    # Transform each path in its absolute version
    topology_dir = os.path.abspath(args[0])
    base_jar = os.path.abspath(options.base_jar)
    output_jar = _build_output_path(options.output_jar, topology_dir)
    if options.pip_log is not None:
        options.pip_log = os.path.abspath(options.pip_log)

    # Check for output path existence for early failure
    if os.path.exists(output_jar):
        e = JarError("Output jar already exist: {0}".format(output_jar))
        sys.exit(PYLEUS_ERROR_FMT.format(PROG, str(e)))

    try:
        # Open the base jar as a zip
        zip_file = _open_jar(base_jar)
    except PyleusError as e:
        sys.exit(PYLEUS_ERROR_FMT.format(PROG, str(e)))

    try:
        # Everything will be copied in a tmp directory
        tmp_dir = tempfile.mkdtemp()
        try:
            _inject(topology_dir, base_jar, output_jar,
                    zip_file, tmp_dir, options)
        except PyleusError as e:
            sys.exit(PYLEUS_ERROR_FMT.format(PROG, str(e)))
        finally:
            shutil.rmtree(tmp_dir)
    finally:
        zip_file.close()


if __name__ == "__main__":
    main()