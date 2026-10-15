# Fastfetch for SatellaOS

A fork of [Fastfetch](https://github.com/fastfetch-cli/fastfetch) 2.69.0 that adds the **SatellaOS logo** and a **one-command build and packaging script** (`.deb`, Arch `.pkg.tar.zst` and `.rpm`), plus a small GUI tool for adding new ASCII logos.

It is part of the [SatellaOS](https://github.com/satellaos-official) ecosystem, but it builds and runs on any Linux distribution.

## Purpose of This Fork

This fork exists because an upstream issue proposing a SatellaOS logo was declined: SatellaOS uses a script-based installation system instead of shipping installation images, so the upstream maintainers did not see a strong reason to include the logo. The fork lets SatellaOS-related changes be developed independently.

What it adds on top of upstream Fastfetch:

- **SatellaOS logo** (`src/logo/ascii/s/satellaos.txt`), registered in `src/logo/ascii/s.inc` under the names `satellaos` and `SatellaOS`, with a purple (`127;63;191`) and white logo and purple keys and title.
- **`build.sh`**: a single script that installs dependencies, compiles and creates a package for Debian, Arch Linux or Fedora.
- **Packaging files**: `package/DEBIAN/control`, `PKGBUILD` and `fastfetch.spec`.
- **`fastfetch-editor.py`**: an optional PyQt6 tool for adding, editing and deleting ASCII logos in the source tree.
- **Documentation** of the whole workflow, so it is easy to start from and modify.

Try the logo after building:

```bash
./build/fastfetch --logo satellaos
```

## Usage

```bash
./build.sh <action> [options]
```

### Actions

| Parameter        | Description                                                                                     |
|------------------|-------------------------------------------------------------------------------------------------|
| `--build-binary` | Compile the source code (add `--apt`, `--dnf` or `--pacman` to install dependencies first).     |
| `--package`      | Install dependencies, compile and create a package. Requires exactly one format option (below). |

### Package formats (only with `--package`)

| Parameter | Description                                                      |
|-----------|------------------------------------------------------------------|
| `--deb`   | `.deb` package for Debian-based systems (dependencies via `apt`) |
| `--pkg`   | Arch Linux package (dependencies via `pacman`)                   |
| `--rpm`   | `.rpm` package for Fedora (dependencies via `dnf`)               |

### Options

| Parameter                    | Description                                                                                                  |
|------------------------------|--------------------------------------------------------------------------------------------------------------|
| `--install`                  | Install the package right after building it (`apt install`, `pacman -U` or `dnf install`). Only valid with `--package`. |
| `--apt`, `--dnf`, `--pacman` | Install the build dependencies with that package manager before compiling. Only valid with `--build-binary`. |
| `-h`, `--help`               | Show the usage information.                                                                                  |

### Examples

```bash
./build.sh --build-binary                  # compile the source code
./build.sh --build-binary --apt            # install dependencies with apt, then compile
./build.sh --build-binary --dnf            # install dependencies with dnf, then compile
./build.sh --build-binary --pacman         # install dependencies with pacman, then compile
./build.sh --package --deb                 # build a .deb package
./build.sh --package --pkg                 # build an Arch package
./build.sh --package --rpm                 # build an .rpm package
./build.sh --package --deb --install       # build and install the .deb package
./build.sh --package --pkg --install       # build and install the Arch package
./build.sh --package --rpm --install       # build and install the .rpm package
```

## Workflow

Each `--package` run does everything in one go:

1. Installs the build dependencies with the distribution's package manager:
   - Debian: `sudo apt install --install-recommends build-essential cmake pkgconf dpkg-dev`
   - Arch: `sudo pacman -S --needed base-devel cmake pkgconf vulkan-headers`
   - Fedora: `sudo dnf install gcc make cmake pkgconf-pkg-config rpm-build`
2. Compiles the project (CMake, `Release` build) in `/tmp/fastfetch-build/` and copies `fastfetch` and `flashfetch` to `build/`.
3. Builds the package in `/tmp/fastfetch-build/` and writes it to `dist/`.
4. Deletes `/tmp/fastfetch-build/`.
5. With `--install`, installs the finished package.

```bash
./build.sh --package --deb
```

`--build-binary` only compiles (steps 2 and 4), and then runs the freshly built `build/fastfetch` once as a quick test. If `cmake`, `make` or a C compiler is missing it stops and lists the options that install them:

```
./build.sh --build-binary --apt
./build.sh --build-binary --dnf
./build.sh --build-binary --pacman
```

Each of these installs the same toolchain packages as the matching `--package` format (without `dpkg-dev` / `rpm-build`) and then compiles.

## Requirements

| Action            | Required tools                                                                         |
|-------------------|----------------------------------------------------------------------------------------|
| `--build-binary`  | `cmake`, `make`, a C compiler (or `--apt` / `--dnf` / `--pacman` to install them)      |
| `--package --deb` | `apt` (dependencies are installed automatically), `package/DEBIAN/` control files      |
| `--package --pkg` | `pacman` (dependencies are installed automatically), a `PKGBUILD` in the project root  |
| `--package --rpm` | `dnf` (dependencies are installed automatically), a `fastfetch.spec` in the project root |

Dependency installation and `--install` use `sudo`, so you may be prompted for your password.
Run each `--package` format on the matching distribution; the script stops with an error if the required package manager is not found.

## Output

Packages are written to the `dist/` directory as `fastfetch_<version>_<arch>.<extension>`. For the current version (`2.69.0`, `x86_64`):

| Distribution | Output file                                                              |
|--------------|--------------------------------------------------------------------------|
| Debian       | `dist/fastfetch_2.69.0_x86_64.deb`                                       |
| Arch Linux   | `dist/fastfetch_2.69.0_x86_64.pkg.tar.zst` (debug packages are skipped)  |
| Fedora       | `dist/fastfetch_2.69.0_x86_64.rpm`                                       |

The packages install `fastfetch` and `flashfetch` to `/usr/bin`. The version and architecture are set at the top of `build.sh` (`VERSION`, `ARCH`) and must match `package/DEBIAN/control`, `PKGBUILD` and `fastfetch.spec` when you change them. Only `x86_64` is supported by the packaging files.

## Project Layout

```
.
├── build.sh               # build and packaging script
├── fastfetch-editor.py    # optional GUI for adding ASCII logos
├── build/                 # fastfetch and flashfetch binaries only
├── dist/                  # finished packages
├── package/DEBIAN/        # Debian control files
├── PKGBUILD               # Arch packaging recipe
├── fastfetch.spec         # Fedora packaging recipe
├── src/                   # Fastfetch source (SatellaOS logo: src/logo/ascii/s/satellaos.txt)
├── presets/, completions/, doc/, scripts/, tests/   # upstream Fastfetch files
├── CMakeLists.txt         # upstream build configuration
└── LICENSE                # MIT license
```

## Error Handling

The script exits with a non-zero status and prints the usage information when:

- no parameter is given,
- an unknown parameter is given,
- more than one action is given (e.g. `--build-binary --package`),
- no action is given (only options),
- `--package` is given without a format, or with more than one (e.g. `--package --deb --rpm`),
- a format option is used without `--package`,
- `--install` is used with `--build-binary`,
- more than one of `--apt`, `--dnf`, `--pacman` is given,
- `--apt`, `--dnf` or `--pacman` is used with `--package` (packaging installs its dependencies itself).

Once a build starts, `/tmp/fastfetch-build/` is removed on every exit: success, failure or Ctrl+C.

## Verifying the Installation

After installing a package, check it with:

```bash
fastfetch --version
```

## Fastfetch Editor (Optional GUI)

`fastfetch-editor.py` is an optional graphical tool for **adding, editing and deleting ASCII logos** in the Fastfetch source tree, so you do not have to edit the logo files by hand. It is separate from `build.sh` and is not required to build or package Fastfetch. The project folder is chosen at the top of the window and shared by both tabs.

**Add logo tab.** Fill in the form and the tool will:

1. copy the logo text to `src/logo/ascii/<letter>/<name>.txt`, and
2. add a matching entry to `src/logo/ascii/<letter>.inc`.

In the form you choose the folder letter, file name, display name, logo names (up to 9), colors (up to 9, named, 256-color or RGB), keys and title colors, and whether it is a small or alternative logo. A live preview shows the colored logo and the generated `.inc` entry before anything is written. Existing entries are never touched, except an entry with the same name, and only after you confirm replacing it.

**Edit logo tab.** Pick a folder letter, then one of the logos listed for that letter (the list can be filtered by name). You can then:

- **Save changes** to its names, colors, flags and logo text. The entry is rewritten in place, keeping its position and field order, so the diff only shows what you changed. Nothing is written if nothing changed.
- **Delete** the logo: its entry is removed from `<letter>.inc` and its `.txt` file is deleted, after a confirmation that lists exactly what will be removed. If the same logo text is still used by an entry in another `.inc` file (for example `macos`), the `.txt` file is kept.
- **Revert** the form to what is on disk.

The folder letter and file name of an existing logo cannot be changed here; delete it and add it again to move or rename it. Entries marked `(!)` in the list contain something the editor cannot reproduce exactly (for example comments inside the entry). For those, the entry details are read-only, so nothing is lost, but you can still edit the logo text or delete them.

After adding or changing a logo, rebuild Fastfetch and try it with `fastfetch --logo <name>`.

The editor requires **Python 3** and **PyQt6**.

### Run the editor

From the project root, run:

```bash
python3 fastfetch-editor.py
```

You can also pass the path of the Fastfetch source folder; without it, the editor looks for the project next to the script and in the current directory, and you can pick the folder with the **Browse...** button:

```bash
python3 fastfetch-editor.py /path/to/fastfetch-2.69.0
```

The script has a Python 3 shebang, so it can also be launched as an executable:

```bash
chmod +x fastfetch-editor.py
./fastfetch-editor.py
```

### Install PyQt6

<details>
<summary>Choose the command for your distribution</summary>

**Debian / Ubuntu and other APT-based distributions**

```bash
sudo apt update
sudo apt install python3-pyqt6
```

**Fedora and other DNF-based distributions**

```bash
sudo dnf install python3-qt6
```

**Arch Linux and other Pacman-based distributions**

```bash
sudo pacman -S python-pyqt6
```

**Using pip**

```bash
python3 -m pip install PyQt6
```

Some distributions prevent installing Python packages into the system Python environment with `pip`. If that happens, use a virtual environment instead:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install PyQt6
python fastfetch-editor.py
```

</details>

## License

Fastfetch is released under the [MIT License](LICENSE). The original copyright notices of the upstream authors (Linus Dierheimer and Carter Li) are kept, and the changes in this fork are released under the same license.

## Upstream Project and Attribution

This project is based on [Fastfetch](https://github.com/fastfetch-cli/fastfetch), originally developed and maintained by the Fastfetch project and its contributors. Credit and thanks go to the upstream developers for their work on Fastfetch.

- **Upstream GitHub:** [fastfetch-cli](https://github.com/fastfetch-cli)
- **Upstream repository:** [fastfetch-cli/fastfetch](https://github.com/fastfetch-cli/fastfetch)

This is an independent fork with additional SatellaOS-related changes. It is not an official Fastfetch project, and the upstream maintainers do not necessarily endorse or support the changes made here.