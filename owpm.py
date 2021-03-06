"""
owpm.py
=======
The core module of owpm, used for everything apart from outside documentation/
build scripts in the scope of owpm.
"""

import hashlib
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from venv import EnvBuilder

import click
import pexpect
import requests
import shellingham
import toml
from packaging.requirements import Requirement
from packaging.version import parse as pkg_parse

"""The current source compatibility level, used for breaking changes to lockfile"""
OWPM_LOCKFILE_VERSION = 1

BUF_SIZE = 65536  # lockfile_hash buffer size

BASE_PATH = Path(os.path.dirname(os.path.abspath(sys.argv[0])))  # Path to owpm dir

VENV_PATH = BASE_PATH / "owpm_venv"  # Path for virtual machines
TOML_PATH = BASE_PATH / "owpm_venv.toml"  # Path for toml cache
TEMP_REQUIRE = (
    BASE_PATH / "owpm_temp_require.txt"
)  # Path for temporary requirements.txt


class ExceptionApiDown(Exception):
    """When a seemingly correct API request to a package repo does not return status 200"""

    pass


class ExceptionVersionError(Exception):
    """When a user gives a version of a package that is not avalible"""

    pass


class ExceptionOwpmNotFound(Exception):
    """When a detection function cannot find a .owpm file"""

    pass


class ExceptionBadOs(Exception):
    """When the user has an incompatible os"""

    pass


class ExceptionVenvInactive(Exception):
    """When trying to modify a venv (from [OwpmVenv]) but it is inactive/does not exist"""

    pass


class ExceptionPackageNotFound(Exception):
    """When a package could not be found inside of pypi/other package repos"""

    pass


class ExceptionOldLockfileSpec(Exception):
    """When the lockfile version/specification is too new or old for owpm to recognise"""

    pass


class ExceptionVenvNotFound(Exception):
    """When the given venv is not found, this should ususally result in a recoverable
    circumstance"""

    pass


class ExceptionCorruptPackage(Exception):
    """When downloading a package, the package was corrupted and doesnt match the
    hash previously downloaded"""

    pass


class OwpmVenv:
    """A built virtual enviroment created from a valid [Project]. If no venv_pin
    is given, it will generate a new one automatically"""

    def __init__(self, venv_pin: int = None, is_active: bool = False):
        if venv_pin is None:
            self.pin = self._get_pin()
        else:
            self.pin = venv_pin

        self.path = self._get_path(self.pin)  # NOTE: could make faster

        if self.path.exists():  # if venv is active
            self.is_active = True
        else:
            self.is_active = is_active

    def __repr__(self):
        return f"venv-{self.pin}"

    def create_venv(self):
        """Creates venv"""

        EnvBuilder(system_site_packages=True).create(self.path)
        self.is_active = True

    def delete(self):
        """Deletes venv if active"""

        if self.is_active and self.path.exists():
            shutil.rmtree(self.path)
            self.is_active = False
        else:
            raise ExceptionVenvInactive(
                "This venv is inactive and so cannot be deleted!"
            )

    def spawn_shell(self, args: list):
        """Creates an interactive shell and injects command base into"""

        if not self.path.exists():
            raise ExceptionVenvNotFound(f"{self} not found!")

        cmd, suffix = self._get_spawn_os()

        activation = f"{cmd} {self.path}/bin/activate{suffix}"
        t_size = self._get_terminal_size()

        shell = pexpect.spawn("bash", ["-i"], dimensions=t_size)
        shell.sendline(activation)

        if len(args) > 0:
            shell.sendline(args)

        shell.interact(escape_character=None)

        # on exit
        shell.close()
        sys.exit(shell.exitstatus)

    def _find_default_shell(self) -> str:
        """Returns the default shell if any, used when shellingham fails"""

        if os.name == "posix":
            cmd = os.environ["SHELL"]
            name = cmd.split("/")[-1]

            return (cmd, name)
        elif os.name == "nt":
            return (os.environ["COMSPEC"], "")

        raise ExceptionBadOs(f"Your {os.name} system is not supported!")

    def _get_spawn_os(self) -> tuple:
        """Returns tuple about os-specific actions related to self.spawn_shell"""

        try:
            found_shell = shellingham.detect_shell()
        except shellingham.ShellDetectionFailure:
            found_shell = self._find_default_shell()
        except:
            return ExceptionBadOs(f"Could not detect shell for your {os.name} system!")

        if found_shell[0] in ("bash", "zsh"):
            return (".", "")
        elif found_shell[0] == "csh":
            return ("source", ".csh")
        elif found_shell[0] == "fish":
            return ("source", ".fish")

        raise ExceptionBadOs(f"Your {found_shell[0]} shell is not supported by owpm!")

    def _get_terminal_size(self) -> tuple:
        """Gets the terminal size for current running. FIXME: this is currently
        linux only and may not work for edge-cases on even linux."""

        return tuple(
            [int(i) for i in os.popen("stty size", "r").read().split()]
        )  # apologies for spaghetti

    def _get_path(self, pin: int) -> Path:
        """Makes a venv path from a specified PIN"""

        return VENV_PATH / str(pin)

    def _get_pin(self) -> int:
        """Randomly generates an unused PIN for a new venv"""

        for attempt in range(45):
            venv_pin = str(random.randint(0, 999))

            if not os.path.exists(self._get_path(venv_pin)):
                return venv_pin


class Project:
    """The overall project file. Name is the save name and lockfile_hash is for stopping mutliple locks on add -> install"""

    def __init__(
        self,
        name: str,
        desc: str = "No description",
        version: str = "0.1.0",
        lockfile_hash: str = "",
    ):
        self.name = name
        self.desc = desc
        self.version = version
        self.lockfile_hash = lockfile_hash
        self.packages = []

    def __repr__(self):
        return f"<name:'{self.name}', desc:'{self.desc}', version:{self.version}, packages:{len(self.packages)}>"

    def save_proj(self):
        """Creates a human-readable and editable save file"""

        save_path = Path(f"{self.name}.owpm")

        payload = {
            "desc": self.desc,
            "version": self.version,
            "lockfile_hash": self.lockfile_hash,
            "packages": {},
        }

        for package in self.packages:
            if package.is_dev:
                if "dev-packages" not in payload:
                    payload["dev-packages"] = {}  # ensure optional is created

                payload["dev-packages"][package.name] = package.version_req
            else:
                payload["packages"][package.name] = package.version_req

        with open(save_path, "w+") as file:
            toml.dump(payload, file)

    def lock_proj(self, force_lock: bool = False) -> bool:
        """Locks all packages and package deps then saves to .owpmlock path;
        `force` always locks, even if owpm thinks the packages are already locked.
        Will return a False if it needed to lock or True if smart-locked"""

        lock_path = Path(f"{self.name}.owpmlock")

        if not force_lock and lock_path.exists() and self._compare_lock_hash(lock_path):
            return True

        _del_path(lock_path)  # delete db

        conn, c = _new_lockfile_connection(lock_path)

        c.execute(
            "CREATE TABLE lock ( name text, version text, hash text, is_dev int, is_dep int )"
        )  # add main lock table
        c.execute(
            f"PRAGMA user_version = {OWPM_LOCKFILE_VERSION}"
        )  # add mark of compatibility

        conn.commit()
        conn.close()

        # adds all deps of package
        for package in self.packages.copy():  # NOTE: also thread this potentially?
            package.get_subpackages()

        # add to db loop around
        for package in self.packages:
            lock_thread = threading.Thread(
                target=package._nthread_lock_package, args=(lock_path,)
            )
            lock_thread.start()

        # TODO: fix race condition that worserns the more locks
        race_conditon_workaround = len(self.packages) / 20
        print(f"\tWaiting {race_conditon_workaround} second(s) for package lock..")
        time.sleep(race_conditon_workaround)

        self._update_lockfile_hash(lock_path)  # add new lockfile to x.owpm

        return False

    def build_proj(
        self, force_lock: bool = False, use_dev_deps: bool = True
    ) -> OwpmVenv:
        """Returns an installed venv or installs packages from lock_path, locks
        if lockfile is out of date and adds to a new venv, which is then returned
        for user to remember"""

        lock_path = Path(f"{self.name}.owpmlock")

        self.lock_proj(force_lock)  # ensure project is locked

        venv_info = _get_venv_status()

        if venv_info:
            venv_path = VENV_PATH / venv_info["pin"]

            if (
                venv_info["lockfile_hash"] != self.lockfile_hash
            ):  # delete old venv that used to be new
                old_venv = OwpmVenv(venv_info["pin"], True)
                print(f"\tDeleting old {old_venv}..")

                try:
                    old_venv.delete()
                except ExceptionVenvInactive:
                    pass  # venv may have been manually deleted

            if not venv_path.exists():
                _set_venv_status({})  # set a new dict as venv is corrupt
            elif (
                venv_info["force_lock"] == force_lock
                and venv_info["use_dev_deps"] == use_dev_deps
                and venv_info["lockfile_hash"] == self.lockfile_hash
            ):
                return OwpmVenv(venv_info["pin"], True)
        else:
            _set_venv_status({})  # set a new dict

        venv = OwpmVenv()
        venv.create_venv()

        conn, c = _new_lockfile_connection(lock_path)

        _verify_lockfile_version(
            c.execute("PRAGMA user_version").fetchall()[0][0]
        )  # ensure lockfile is to owpm's spec

        if not use_dev_deps:
            select_query = "SELECT * FROM lock WHERE is_dep=0"
        else:
            select_query = "SELECT * FROM lock WHERE is_dev=0 AND is_dep=0"

        found_non_deps = c.execute(
            select_query
        ).fetchall()  # find all non-dep packages and filter for use_dev_deps

        # install all non-dep packages
        for non_dep in found_non_deps:
            print(f"\tInstalling '{non_dep[0]}':{non_dep[1]}..")

            with open(TEMP_REQUIRE, "w+") as f_out:
                f_out.write(f"{non_dep[0]} --hash=sha256:{non_dep[2]}\n")

            command_to_call = [
                f"{venv.path}/bin/python",
                "-m",
                "pip",
                "install",
                "-r",
                TEMP_REQUIRE,
                "--require-hashes",
            ]

            cmd_out = subprocess.call(command_to_call, stdout=subprocess.DEVNULL)

            if cmd_out != 0: # TODO: This will accidently flag errors that are just installation errors
                raise ExceptionCorruptPackage(
                    f"Package that was installing is corrupt/tampered! Please ensure your using stable & reliable internet then try again shortly."
                )

        conn.close()

        _set_venv_status(
            {
                "pin": venv.pin,
                "lockfile_hash": self.lockfile_hash,
                "force_lock": force_lock,
                "use_dev_deps": use_dev_deps,
            }
        )  # caching mechanism so build_proj can skip installs if exactly the same

        return venv

    def remove_packages(self, to_remove: list):
        """Removes a list of [Package] from .owpm"""

        for package in to_remove:
            if type(package) != Package:
                raise Exception(
                    f"Trying to remove '{package}' from db which is not a [Package]!"
                )

            print(f"\tRemoving {package}")

            self.packages.remove(package)

        self.lockfile_hash = ""  # ensure lock
        self.save_proj()

    def remove_cached_venv(self):
        """Removes a cached venv from the venv cache status if existant. Ensure
        this is used before wiping any lockfile hashes as it requires one to
        identify itself."""

        # TODO: restructure venv cache to allow multiple enviroments at once

        venv_status = _get_venv_status()

        if len(venv_status) != 0 and venv_status["lockfile_hash"] == self.lockfile_hash:
            print("Removing cached virtual enviroment..")

            rem_venv = OwpmVenv(venv_status["pin"], True)
            rem_venv.delete()

            _set_venv_status({})

    def _compare_lock_hash(self, lock_path: Path) -> bool:
        """Compares self.lockfile_hash with a newly generated hash from the actual lockfile"""

        return self._hash_lockfile(lock_path) == self.lockfile_hash

    def _hash_lockfile(self, lock_path: Path) -> str:
        """Hashes a lockfile to use in comparisons or at end of locking"""

        md5 = hashlib.md5()

        with open(lock_path, "rb") as file:
            while True:
                data = file.read(BUF_SIZE)

                if not data:
                    break

                md5.update(data)

        return md5.hexdigest()

    def _update_lockfile_hash(self, lock_path: Path):
        """Updates lockfile hash and saves it to a .owpm file (doesn't save all
        in self.packages)"""

        save_path = Path(f"{self.name}.owpm")

        self.lockfile_hash = self._hash_lockfile(lock_path)

        payload = toml.load(open(save_path, "r"))
        payload["lockfile_hash"] = self.lockfile_hash
        toml.dump(payload, open(save_path, "w+"))


class Package:
    """A single package when using owpm. `save_hash` is generated automatically
    after locking once. should_rem_hash is internal use on loading from .owpm
    files and is_dev defines if it is a development package or not"""

    def __init__(
        self,
        parent_proj: Project,
        name: str,
        version_req: str = "*",
        is_dev: bool = False,
        is_dep: bool = False,
        should_rem_hash: bool = True,
    ):
        self.name = name
        self.version_req = version_req
        self.parent_proj = parent_proj
        self.is_dev = is_dev
        self.is_dep = is_dep

        self.parent_proj.packages.append(self)

        if should_rem_hash:
            self.parent_proj.lockfile_hash = ""  # ensure locks

    def __repr__(self):
        if (
            self.version_req == "*" or not self.is_dep
        ):  # latest version set/defined package
            repr_version = self.version_req
        else:  # if it is using pypi requirements
            version_split = self.version_req.split(" ;")[0].split(" ")

            if len(version_split) == 1:
                repr_version = "*"
            else:
                repr_version = version_split[1][1:-1]

        return f"'{self.name}':{repr_version}"

    def get_subpackages(self) -> str:
        """Scans pypi for dependancies and adds them to [Project.package] as a
        new [Package] and returns hash of this self"""

        print(f"\tPulling deps for {self}..")

        resp = _pypi_req(self.name)
        resp_json = resp.json()

        required = resp_json["info"]["requires_dist"]

        if required:  # API gives NoneType sometimes
            for subpackage in required:
                subpkg_name = subpackage.split(";")[0].split(" ")[0]

                # make new packages into parent [Project] using same is_dev as self
                Package(self.parent_proj, subpkg_name, subpackage, self.is_dev, True)

        return self.get_hash(resp)

    def get_hash(self, package_resp: requests.Response) -> str:
        """Gets lock hash, package_resp is for modularity (use `_pypi_req("package")`)"""

        resp_json = package_resp.json()

        if self.version_req == "*":
            if len(resp_json["urls"]) == 0:
                raise ExceptionVersionError(
                    f"Package {self} has been created too recently for pypi to compute!"
                )

            return resp_json["urls"][0]["digests"]["sha256"]

        version_requires = Requirement(self.version_req)

        for version_string in resp_json["releases"]:
            parsed_version = pkg_parse(
                version_string
            )  # parse already-valid pypi into package.version.Version

            content_body = resp_json["releases"][version_string]

            # if parsed_version matches required
            if parsed_version in version_requires.specifier and len(content_body) > 0:
                # TODO: make sure that release even has content, if not install version before
                return content_body[0]["digests"]["sha256"]

        raise ExceptionVersionError(
            f"Package {self} with this specific version could not be found in pypi!"
        )  # if package was not returned by for loop

    def _nthread_lock_package(self, lock_path: Path):
        """Designed for a multi-threaded locking system to add a single package"""

        print(f"\tLocking {self}..")

        conn, c = _new_lockfile_connection(lock_path)

        made_hash = self.get_hash(_pypi_req(self.name))

        if c.execute(f"SELECT * FROM lock WHERE hash='{made_hash}'").fetchall():
            return  # hash already in lock, no need to add twicee

        if self.is_dep:
            c.execute(
                "INSERT INTO lock VALUES ( ?, ?, ?, ?, 1 )",
                (self.name, self.version_req, made_hash, self.is_dev),
            )
        else:
            c.execute(
                "INSERT INTO lock VALUES ( ?, ?, ?, ?, 0 )",
                (self.name, self.version_req, made_hash, self.is_dev),
            )

        conn.commit()
        conn.close()


def project_from_toml(owpm_path: Path) -> Project:
    """Gets a [Project] from a given TOML path"""

    payload = toml.load(open(owpm_path, "r"))

    project = Project(
        owpm_path.stem, payload["desc"], payload["version"], payload["lockfile_hash"]
    )

    for package in payload["packages"]:
        new_package = Package(
            project, package, payload["packages"][package], False, False, False
        )

    # optional development packages
    if "dev-packages" in payload:
        for package in payload["dev-packages"]:
            new_package = Package(
                project, package, payload["dev-packages"][package], True, False, False
            )

    return project


def first_project_indir() -> Project:
    """Finds first .owpm file in running directory and returns [Project]"""

    found = False

    for file in os.listdir("."):
        if file.endswith(".owpm"):
            found = True
            return project_from_toml(Path(file))

    if not found:
        raise ExceptionOwpmNotFound("An .owpm file was not found in the current path!")


def _del_path(file_path: Path):
    """Deletes given file path if it exists"""

    if file_path.exists():
        os.remove(file_path)


def _verify_lockfile_version(user_version: int) -> bool:
    """Verifies lockfile using local tally of user_version to ensure compatibility"""

    if user_version != OWPM_LOCKFILE_VERSION:  # NOTE: could be not hardcoded in future
        raise ExceptionOldLockfileSpec(
            "The .owpmlock version/specification is too new or old for owpm to recognise!"
        )


def _new_lockfile_connection(lock_path: Path) -> tuple:
    """Creates a new sqlite connection to a given lockfile"""

    conn = sqlite3.connect(str(lock_path))
    c = conn.cursor()

    return (conn, c)


def _pypi_req(package: str) -> requests.Response:
    """Constructs a fully-formed json request to the PyPI API using a given
    package name"""

    resp = requests.get(f"https://pypi.org/pypi/{package}/json")

    if resp.status_code == 200:
        return resp
    elif resp.status_code == 404:
        raise ExceptionPackageNotFound(
            f"The package '{package}' was not found in pypi!"
        )
    else:
        raise ExceptionApiDown(
            f"A seemingly valid API request to PyPI has failed with error #{resp.status_code}!"
        )


def _set_venv_status(arg: dict):
    """Sets the current venv inside of owpm data dir like owpm_venv"""

    with open(TOML_PATH, "w+") as file:
        toml.dump(arg, file)


def _get_venv_status() -> dict:
    """Gets status of venv, if any are active"""

    if TOML_PATH.exists():
        with open(TOML_PATH, "r") as file:
            return toml.load(file)
    else:
        _set_venv_status({})
        return {}  # we know it's empty so no need to open again


@click.group()
def base_group():
    pass


@click.command()
@click.option("--name", help="Name of project", prompt="Name of your project")
@click.option(
    "--desc",
    help="Breif description",
    prompt="Breif description/overview",
    default="No description",
)
@click.option("--ver", help="Base version of project (default 0.1.0)", default="0.1.0")
def init(name, desc, ver):
    """Creates a new .owpm project file"""

    print("Initializing..")

    new_proj = Project(name, desc, ver)
    new_proj.save_proj()

    print(f"Saved project as '{name}.owpm'!")


@click.command()
@click.argument("names", nargs=-1, required=True)
@click.option(
    "--dev",
    "-d",
    help="Saves all packages as development packages (not used with build --publish)",
    is_flag=True,
    default=False,
)
def add(names, dev):
    """Interactively adds a package to .owpm and saves .owpm"""

    proj = first_project_indir()
    proj.remove_cached_venv()

    if dev:
        print("Adding development package(s)..")
    else:
        print("Adding package(s)..")

    for package in names:
        package_info = package.split("==")

        if len(package_info) == 1:
            package_info.append("*")  # if no version is set, add latest

        new_package = Package(proj, package_info[0], package_info[1], dev)
        print(f"\tAdded {new_package}!")

    proj.save_proj()

    print(f"Project saved to '{proj.name}.owpm' with {len(names)} package(s) added!")


@click.command()
@click.argument("names", nargs=-1, required=True)
@click.option(
    "--dev",
    "-d",
    help="Removes development packages instead of normal ones",
    is_flag=True,
    default=False,
)
def rem(names, dev):
    """Removes provided package(s). this is interactive and may have dupe packages
    with differing versions"""

    proj = first_project_indir()
    proj.remove_cached_venv()

    found = []

    if dev:
        print("Removing development package(s)..")
    else:
        print("Removing package(s)..")

    removed_any_pkg = False

    for package in proj.packages:
        if package.name in names and package.is_dev == dev:
            found.append(
                package
            )  # TODO: make sure it doesnt delete 2 different verions with same name
            removed_any_pkg = True

    proj.remove_packages(found)

    if removed_any_pkg:
        print(f"Removed {len(found)} package(s) from '{proj.name}.owpm' and lockfile!")
    else:
        compiled_names = ", ".join(
            [f"'{name}'" for name in names]
        )  # makes tuple into `'x', 'y', 'z'`

        print(f"No packages named {compiled_names} where found so nothing was removed!")


@click.command()
@click.option(
    "--force",
    "-f",
    help="Forces a lock, even if lock is seemingly up-to-date",
    is_flag=True,
    default=False,
)
def lock(force):
    """Locks the first found .owpm file"""

    proj = first_project_indir()

    print("Locking project..")

    smart_locked = proj.lock_proj(force)

    if smart_locked:
        print(
            f"Did not lock project, '{proj.name}.owpmlock' is up-to-date! (try -f for forceful lock)"
        )
    else:
        print(f"Locked project as '{proj.name}.owpmlock'!")


@click.command()
@click.option("--pin", "-p", help="Custom virtual enviroment PIN", required=False)
@click.option(
    "--force",
    "-f",
    help="Forces a lock, even if lock is seemingly up-to-date",
    is_flag=True,
    default=False,
)
@click.option(
    "--publish",
    help="Makes build into a 'published' build with no development deps being used",
    is_flag=True,
    default=False,
)
@click.argument("args", nargs=-1)
def run(pin, force, publish, args):
    """Starts an interactive virtual enviroment or a temporary enviroment using
    args given. If a custom PIN is given, it won't use the current virtual
    enviroment cache"""

    proj = first_project_indir()

    print("Acquiring venv..")

    if pin is None:
        venv = proj.build_proj(force, publish)
    else:
        venv = OwpmVenv(pin)

        if not venv.path.exists():
            print("\tGiven pin doesn't exist, creating new venv!")
            venv = proj.build_proj(force, publish)

    # conn, c = _new_lockfile_connection(Path(f"{proj.name}.owpmlock"))
    # venv.check_venv_hashes(c)

    print("Starting venv..")

    venv.spawn_shell(" ".join(args))

    print(f"Finished running {venv}!")


@click.command()
@click.option(
    "--force",
    "-f",
    help="Forces a lock, even if lock is seemingly up-to-date",
    is_flag=True,
    default=False,
)
@click.option(
    "--publish",
    "-p",
    help="Makes build into a 'published' build with no development deps being used",
    is_flag=True,
    default=False,
)
def build(force, publish):
    """Constructs a new venv and provides the PIN"""

    proj = first_project_indir()

    if publish:
        print("Constructing new production venv..")
    else:
        print("Constructing new development venv..")

    venv = proj.build_proj(force, publish)

    print(f"Created {venv}!")


@click.command()
@click.option(
    "--pin", "-p", help="Pin wanted for removal", prompt="Pin to remove", type=int
)
def venv_rem(pin, all):
    """Deletes specified venv pin"""

    venv = OwpmVenv(pin, True)

    print(f"Deleting {venv}..")
    venv.delete()
    print(f"Deleted {venv}!")


@click.command()
def clean():
    """Removes all virtual enviroments and cache to sort out any malfunctions"""

    if VENV_PATH.exists():
        print("Removing all venvs..")

        shutil.rmtree(VENV_PATH)
    else:
        print("No venvs to remove!")

    if TOML_PATH.exists():
        print("Removing cahce..")

        os.remove(TOML_PATH)
    else:
        print("No cache to remove!")


@click.command()
def venv_list():
    """Lists all active venv files"""

    print("Finding venvs..")

    not_found_err = "No venvs found, you may make one using `owpm build`!"
    venv_count = 0

    if VENV_PATH.exists():
        for venv in os.listdir(VENV_PATH):
            venv_count += 1

            print(f"\t{OwpmVenv(int(venv))}")

        if venv_count == 0:
            print(not_found_err)
        else:
            print(f"Found {venv_count} venv(s)!")
    else:
        print(not_found_err)


@click.command()
@click.option(
    "--lockfile",
    "-l",
    help="Lists all packages and all dependancies from lockfile",
    is_flag=True,
    default=False,
)
def pkg_list(lockfile):
    """Lists all packages of first found .owpm file"""

    proj = first_project_indir()

    if lockfile:
        print("Force-locking project..")
        proj.lock_proj(True)
        print("Project has been force-locked!")

        print("Listing dependancies..")

        for package in proj.packages:
            if package.is_dep:
                print(f"\t{package}")

        print(f"Found dependancies of count {len(proj.packages)-1}!")
    else:
        packages = []
        dev_packages = []

        for package in proj.packages:
            if package.is_dev:
                dev_packages.append(str(package))
            else:
                packages.append(str(package))

        if len(packages) == 0:
            print("No normal packages found, you can use `owpm add` to add some!")
        else:
            print("Listing packages..")

            for package in packages:
                print(f"\t{package}")

            print(f"Found {len(packages)} package(s)!")

        if len(dev_packages) == 0:
            print(
                "No development packages found, you can use `owpm add -d` to add some!"
            )
        else:
            print("Listing development packages..")

            for dev_package in dev_packages:
                print(f"\t{dev_package}")

            print(f"Found {len(dev_packages)} development package(s)!")


base_group.add_command(init)
base_group.add_command(lock)

base_group.add_command(add)
base_group.add_command(rem)
base_group.add_command(pkg_list)

base_group.add_command(build)
base_group.add_command(run)
base_group.add_command(clean)

base_group.add_command(venv_list)
base_group.add_command(venv_rem)

if __name__ == "__main__":
    base_group()
