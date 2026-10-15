#!/usr/bin/env bash
#
# Unified Fastfetch build & packaging script
#
# Usage:
#   build.sh --build-binary [--apt|--dnf|--pacman]   Compile (optionally install deps first)
#   build.sh --package --deb [--install]       Install deps, compile, package for Debian (and install)
#   build.sh --package --pkg [--install]       Install deps, compile, package for Arch Linux (and install)
#   build.sh --package --rpm [--install]       Install deps, compile, package for Fedora (and install)
#
# All intermediate files (CMake cache, object files, package staging trees,
# makepkg/rpmbuild work dirs) live in /tmp/fastfetch-build/ and that directory
# is removed when the script exits. The only things left in the project are:
#   build/fastfetch, build/flashfetch   the compiled binaries
#   dist/                               the finished packages

# Exit immediately if a command exits with a non-zero status
set -e

# Get the directory where the script is located without changing directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Configuration variables
WORK_DIR="/tmp/fastfetch-build"          # every temporary file goes here
CMAKE_DIR="$WORK_DIR/cmake"              # CMake build tree
BIN_DIR="$SCRIPT_DIR/build"              # final binaries only
DIST_DIR="$SCRIPT_DIR/dist"
PACKAGE_SRC_DIR="$SCRIPT_DIR/package"    # Debian package template (DEBIAN/, ...)
SPEC_FILE="$SCRIPT_DIR/fastfetch.spec"
PKGBUILD_FILE="$SCRIPT_DIR/PKGBUILD"
APP_NAME="fastfetch"
VERSION="2.69.0"
ARCH="x86_64"

MODE=""
FORMAT=""
INSTALL_FLAG=false
PM_FLAG=""

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

# Full usage guide, printed only by -h / --help.
# Parameters and commands are shown in green when the output is a terminal
# (no colors when the output is piped or redirected, or when NO_COLOR is set).
usage() {
    local G="" N=""
    if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
        G=$'\033[32m'
        N=$'\033[0m'
    fi

    cat <<EOF
Fastfetch Build & Packaging
===========================

USAGE
  ${G}./build.sh <action> [options]${N}
  ${G}bash build.sh <action> [options]${N}

ACTIONS (exactly one is required)
  ${G}--build-binary${N}        Compile the source code. Dependencies are not installed unless
                        ${G}--apt${N}, ${G}--dnf${N} or ${G}--pacman${N} is also given.
  ${G}--package${N}             Install dependencies, compile and create a package.
                        Requires exactly one format option (below).

PACKAGE FORMATS (only valid with ${G}--package${N}, exactly one is required)
  ${G}--deb${N}                 .deb package for Debian-based systems (dependencies via apt)
  ${G}--pkg${N}                 Arch Linux package (dependencies via pacman)
  ${G}--rpm${N}                 .rpm package for Fedora (dependencies via dnf)

OPTIONS
  ${G}--install${N}             Install the package right after building it
                        (${G}apt install${N} / ${G}pacman -U${N} / ${G}dnf install${N}).
                        Only valid with ${G}--package${N}.
  ${G}--apt${N} | ${G}--dnf${N} | ${G}--pacman${N}
                        Install the build dependencies with that package manager
                        before compiling. Only valid with ${G}--build-binary${N}.
  ${G}-h${N}, ${G}--help${N}            Show this guide.

EXAMPLES
  ${G}./build.sh --build-binary${N}                  Compile the source code
  ${G}./build.sh --build-binary --apt${N}            Install dependencies with apt, then compile
  ${G}./build.sh --build-binary --dnf${N}            Install dependencies with dnf, then compile
  ${G}./build.sh --build-binary --pacman${N}         Install dependencies with pacman, then compile
  ${G}./build.sh --package --deb${N}                 Build a .deb package
  ${G}./build.sh --package --pkg${N}                 Build an Arch package
  ${G}./build.sh --package --rpm${N}                 Build an .rpm package
  ${G}./build.sh --package --deb --install${N}       Build and install the .deb package
  ${G}./build.sh --package --pkg --install${N}       Build and install the Arch package
  ${G}./build.sh --package --rpm --install${N}       Build and install the .rpm package

WORKFLOW
  Each ${G}--package${N} run does everything in one go:
    1. Installs the build dependencies with the distribution's package manager:
         Debian: ${G}sudo apt install --install-recommends build-essential cmake pkgconf dpkg-dev${N}
         Arch:   ${G}sudo pacman -S --needed base-devel cmake pkgconf vulkan-headers${N}
         Fedora: ${G}sudo dnf install gcc make cmake pkgconf-pkg-config rpm-build${N}
    2. Compiles the project (CMake, Release build) in ${WORK_DIR}/ and
       copies fastfetch and flashfetch to build/.
    3. Builds the package in ${WORK_DIR}/ and writes it to dist/.
    4. Deletes ${WORK_DIR}/.
    5. With ${G}--install${N}, installs the finished package.

  ${G}--build-binary${N} only compiles (steps 2 and 4), then runs the freshly built
  build/fastfetch once as a quick test. If cmake, make or a C compiler is missing,
  it stops and lists the options that install them (${G}--apt${N}, ${G}--dnf${N}, ${G}--pacman${N}).
  These install the same toolchain packages as the matching ${G}--package${N} format,
  without dpkg-dev / rpm-build.

REQUIREMENTS
  ${G}--build-binary${N}    cmake, make, a C compiler (or ${G}--apt${N} / ${G}--dnf${N} / ${G}--pacman${N} to install them)
  ${G}--package --deb${N}   apt (dependencies are installed automatically), package/DEBIAN/ control files
  ${G}--package --pkg${N}   pacman (dependencies are installed automatically), PKGBUILD in the project root
  ${G}--package --rpm${N}   dnf (dependencies are installed automatically), fastfetch.spec in the project root

  Dependency installation and ${G}--install${N} use sudo, so you may be prompted for your
  password. Run each ${G}--package${N} format on the matching distribution.

OUTPUT
  Packages are written to dist/ as fastfetch_<version>_<arch>.<extension>:
    Debian      dist/${APP_NAME}_${VERSION}_${ARCH}.deb
    Arch Linux  dist/${APP_NAME}_${VERSION}_${ARCH}.pkg.tar.zst   (debug packages are skipped)
    Fedora      dist/${APP_NAME}_${VERSION}_${ARCH}.rpm
  The binaries fastfetch and flashfetch are copied to build/.
  Everything else is created in ${WORK_DIR}/ and removed when the script exits
  (on success, on failure and on Ctrl+C).

INVALID PARAMETERS
  The script exits with a non-zero status and a short message when:
    - no parameter is given, or an unknown parameter is given,
    - more than one action is given (e.g. ${G}--build-binary --package${N}),
    - no action is given (only options),
    - ${G}--package${N} is given without a format, or with more than one (e.g. ${G}--deb --rpm${N}),
    - a format option is used without ${G}--package${N},
    - ${G}--install${N} is used with ${G}--build-binary${N},
    - more than one of ${G}--apt${N}, ${G}--dnf${N}, ${G}--pacman${N} is given,
    - ${G}--apt${N}, ${G}--dnf${N} or ${G}--pacman${N} is used with ${G}--package${N} (packaging installs its
      dependencies itself).

VERIFYING THE INSTALLATION
  After installing a package, check it with:
    ${G}fastfetch --version${N}
EOF
}

# Every invalid-parameter case ends here. The reason ($1) is intentionally not
# printed: the script only points the user to the guide (--help).
die_usage() {
    local G="" N=""
    if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
        G=$'\033[32m'
        N=$'\033[0m'
    fi

    cat >&2 <<EOF
Invalid parameter, please read the guide for more information.

With the Bash command
${G}╭────────────────────────╮
│ \$ bash build.sh --help │
╰────────────────────────╯${N}

With executable permission
${G}╭─────────────────────╮
│ \$ chmod +x build.sh │
│ \$ ./build.sh --help │
╰─────────────────────╯${N}
EOF
    exit 1
}

die() {
    echo "[!] Error: $1" >&2
    exit 1
}

set_mode() {
    if [ -n "$MODE" ]; then
        die_usage "Only one action can be specified (already got --$MODE, found $1)."
    fi
    MODE="${1#--}"
}

# Remove the temporary work directory (runs on every exit, including errors and Ctrl+C)
cleanup() {
    if [ -d "$WORK_DIR" ]; then
        rm -rf "$WORK_DIR"
    fi
}

prepare_work_dir() {
    rm -rf "$WORK_DIR"
    mkdir -p "$WORK_DIR"
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
}

ensure_dist_dir() {
    if [ ! -d "$DIST_DIR" ]; then
        echo "[-] Creating output directory: ${DIST_DIR}/"
        mkdir -p "$DIST_DIR"
    fi
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "'$1' was not found. Run this action on a $2 system."
}

# ------------------------------------------------------------------
# Dependencies (installed before compiling)
# ------------------------------------------------------------------

# Each function installs the toolchain; the optional argument "package" also
# installs the tools needed to build that distribution's package.

install_deps_debian() {
    require_command apt "Debian-based"
    local pkgs=(build-essential cmake pkgconf)
    [ "${1:-}" = "package" ] && pkgs+=(dpkg-dev)
    echo "[-] Installing build dependencies with apt..."
    sudo apt install --install-recommends "${pkgs[@]}"
}

install_deps_arch() {
    require_command pacman "Arch Linux"
    local pkgs=(base-devel cmake pkgconf vulkan-headers)
    echo "[-] Installing build dependencies with pacman..."
    sudo pacman -S --needed "${pkgs[@]}"
}

install_deps_fedora() {
    require_command dnf "Fedora"
    local pkgs=(gcc make cmake pkgconf-pkg-config)
    [ "${1:-}" = "package" ] && pkgs+=(rpm-build)
    echo "[-] Installing build dependencies with dnf..."
    sudo dnf install "${pkgs[@]}"
}

# Used by --build-binary when a package manager flag is given
install_deps_for_flag() {
    case "$PM_FLAG" in
        apt)    install_deps_debian ;;
        pacman) install_deps_arch ;;
        dnf)    install_deps_fedora ;;
    esac
}

# Verify that the tools needed for compiling exist; if not, list the options
# that install them and stop.
check_build_tools() {
    local missing=()
    command -v cmake >/dev/null 2>&1 || missing+=("cmake")
    command -v make  >/dev/null 2>&1 || missing+=("make")
    if ! command -v cc >/dev/null 2>&1 && ! command -v gcc >/dev/null 2>&1; then
        missing+=("a C compiler (gcc)")
    fi

    if [ "${#missing[@]}" -gt 0 ]; then
        echo "[!] Error: missing build dependencies: ${missing[*]}" >&2
        echo "[!] Install them automatically by choosing the option for your distribution:" >&2
        echo >&2
        echo "    $0 --build-binary --apt" >&2
        echo "    $0 --build-binary --dnf" >&2
        echo "    $0 --build-binary --pacman" >&2
        exit 1
    fi
}

# ------------------------------------------------------------------
# Compilation
# ------------------------------------------------------------------

# Configure and compile inside $WORK_DIR, then copy ONLY the two binaries
# into $BIN_DIR.
compile_binaries() {
    echo "[-] Configuring project with CMake (build tree: $CMAKE_DIR)..."
    cmake -B "$CMAKE_DIR" -S "$SCRIPT_DIR" -DCMAKE_BUILD_TYPE=Release

    echo "[-] Compiling project..."
    cmake --build "$CMAKE_DIR" -j"$(nproc)"

    if [ ! -f "$CMAKE_DIR/fastfetch" ] || [ ! -f "$CMAKE_DIR/flashfetch" ]; then
        die "Compilation finished but the fastfetch/flashfetch binaries were not produced."
    fi

    echo "[-] Copying binaries to ${BIN_DIR}/ ..."
    mkdir -p "$BIN_DIR"
    cp -f "$CMAKE_DIR/fastfetch" "$BIN_DIR/fastfetch"
    cp -f "$CMAKE_DIR/flashfetch" "$BIN_DIR/flashfetch"
}

# ------------------------------------------------------------------
# Actions
# ------------------------------------------------------------------

build_binary() {
    echo "=========================================="
    echo " Starting Fastfetch Build Process"
    echo "=========================================="

    install_deps_for_flag
    check_build_tools
    prepare_work_dir
    compile_binaries

    echo "=========================================="
    echo "[+] Build completed successfully!"
    echo "=========================================="

    # Run the compiled binary to test it
    echo "------------------------------------------"
    echo " Running fastfetch test:"
    echo "------------------------------------------"
    "$BIN_DIR/fastfetch"
}

package_debian() {
    local output_deb="${APP_NAME}_${VERSION}_${ARCH}.deb"
    local stage="$WORK_DIR/deb"

    echo "=========================================="
    echo " Starting Fastfetch DEB Packaging"
    echo "=========================================="

    [ -d "$PACKAGE_SRC_DIR/DEBIAN" ] || die "Missing $PACKAGE_SRC_DIR/DEBIAN (control files)."

    install_deps_debian package
    prepare_work_dir
    compile_binaries
    ensure_dist_dir

    # Build the package tree in the temporary directory, not in the project
    echo "[-] Preparing package structure in $stage ..."
    mkdir -p "$stage/usr/bin"
    cp -a "$PACKAGE_SRC_DIR/DEBIAN" "$stage/DEBIAN"
    cp "$BIN_DIR/fastfetch" "$stage/usr/bin/"
    cp "$BIN_DIR/flashfetch" "$stage/usr/bin/"

    echo "[-] Building the DEB package..."
    dpkg-deb --root-owner-group --build "$stage" "$DIST_DIR/$output_deb"

    echo "=========================================="
    echo "[+] Package successfully created: ${DIST_DIR}/${output_deb}"
    echo "=========================================="

    if [ "$INSTALL_FLAG" = true ]; then
        echo "[-] Installing the DEB package and resolving dependencies..."
        sudo apt install "$DIST_DIR/$output_deb"
        echo "[+] Installation complete. Verify with: fastfetch --version"
    fi
}

package_arch() {
    local stage="$WORK_DIR/arch"
    local target_pkg=""

    echo "=========================================="
    echo " Starting Fastfetch Arch Linux Packaging"
    echo "=========================================="

    [ -f "$PKGBUILD_FILE" ] || die "Missing $PKGBUILD_FILE (Arch packaging recipe)."

    install_deps_arch
    prepare_work_dir
    compile_binaries
    ensure_dist_dir

    # makepkg runs inside the temporary directory: PKGBUILD and the binaries are
    # copied there, and makepkg's own build/package/output dirs are redirected too
    echo "[-] Preparing makepkg directory in $stage ..."
    mkdir -p "$stage/pkgdest" "$stage/builddir" "$stage/srcdest"
    cp "$PKGBUILD_FILE" "$stage/PKGBUILD"
    cp "$BIN_DIR/fastfetch" "$stage/"
    cp "$BIN_DIR/flashfetch" "$stage/"

    echo "[-] Building the Arch package..."
    (
        cd "$stage"
        BUILDDIR="$stage/builddir" \
        PKGDEST="$stage/pkgdest" \
        SRCDEST="$stage/srcdest" \
        makepkg -f
    )

    # Copy generated package to dist/ (filtering out -debug packages) and give it the
    # same naming scheme as the other formats: fastfetch_<version>_<arch>.pkg.tar.<comp>
    echo "[-] Moving package to dist/..."
    local pkg_file pkg_ext
    for pkg_file in "$stage"/pkgdest/fastfetch-*.pkg.tar.*; do
        if [ -f "$pkg_file" ] && [[ "$pkg_file" != *-debug-* ]]; then
            pkg_ext="${pkg_file#*.pkg.tar}"    # e.g. ".zst"
            target_pkg="$DIST_DIR/${APP_NAME}_${VERSION}_${ARCH}.pkg.tar${pkg_ext}"
            cp -f "$pkg_file" "$target_pkg"
        fi
    done

    [ -n "$target_pkg" ] || die "No package was produced by makepkg."

    echo "=========================================="
    echo "[+] Arch Package successfully created in: ${DIST_DIR}/"
    echo "=========================================="

    if [ "$INSTALL_FLAG" = true ]; then
        echo "[-] Installing the Arch package..."
        sudo pacman -U "$target_pkg"
        echo "[+] Installation complete. Verify with: fastfetch --version"
    fi
}

package_fedora() {
    local target_rpm="${DIST_DIR}/${APP_NAME}_${VERSION}_${ARCH}.rpm"
    local rpm_top="$WORK_DIR/rpmbuild"
    local rpm_result_dir="$rpm_top/RPMS/$ARCH"
    local rpm_file found=false

    echo "=========================================="
    echo " Starting Fastfetch RPM Packaging"
    echo "=========================================="

    [ -f "$SPEC_FILE" ] || die "Missing $SPEC_FILE (RPM spec file)."

    install_deps_fedora package
    prepare_work_dir
    compile_binaries
    ensure_dist_dir

    # Prepare standard RPM directory structure in the temporary directory
    echo "[-] Setting up local RPM build environment..."
    mkdir -p "$rpm_top"/{BUILD,RPMS,SOURCES,SPECS,SRPMS,rpmdb}
    cp "$BIN_DIR/fastfetch" "$rpm_top/SOURCES/"
    cp "$BIN_DIR/flashfetch" "$rpm_top/SOURCES/"

    # Build the RPM package using local database path
    echo "[-] Building the RPM package..."
    rpmbuild -bb \
        --define "_topdir $rpm_top" \
        --define "_dbpath $rpm_top/rpmdb" \
        "$SPEC_FILE"

    # Copy and rename generated rpm to dist/
    for rpm_file in "$rpm_result_dir"/*.rpm; do
        if [ -f "$rpm_file" ]; then
            cp -f "$rpm_file" "$target_rpm"
            found=true
        fi
    done

    [ "$found" = true ] || die "No RPM was produced by rpmbuild."

    echo "=========================================="
    echo "[+] RPM Package successfully created in: ${DIST_DIR}/"
    echo "=========================================="

    if [ "$INSTALL_FLAG" = true ]; then
        echo "[-] Installing the RPM package..."
        sudo dnf install "$target_rpm"
        echo "[+] Installation complete. Verify with: fastfetch --version"
    fi
}

# ------------------------------------------------------------------
# Argument parsing (running without parameters is not allowed)
# ------------------------------------------------------------------

if [ "$#" -eq 0 ]; then
    die_usage "No parameters given. An action is required."
fi

for arg in "$@"; do
    case "$arg" in
        --build-binary|--package)
            set_mode "$arg"
            ;;
        --deb|--pkg|--rpm)
            if [ -n "$FORMAT" ]; then
                die_usage "Only one of --deb, --pkg, --rpm can be specified."
            fi
            FORMAT="${arg#--}"
            ;;
        --install)
            INSTALL_FLAG=true
            ;;
        --apt|--dnf|--pacman)
            if [ -n "$PM_FLAG" ]; then
                die_usage "Only one of --apt, --dnf, --pacman can be specified."
            fi
            PM_FLAG="${arg#--}"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die_usage "Unknown parameter: $arg"
            ;;
    esac
done

if [ -z "$MODE" ]; then
    die_usage "An action is required (--build-binary or --package)."
fi

if [ "$MODE" = "package" ] && [ -z "$FORMAT" ]; then
    die_usage "--package requires a format: --deb, --pkg or --rpm."
fi

if [ "$MODE" != "package" ] && [ -n "$FORMAT" ]; then
    die_usage "--$FORMAT can only be used with --package."
fi

if [ "$INSTALL_FLAG" = true ] && [ "$MODE" = "build-binary" ]; then
    die_usage "--install can only be used with --package."
fi

if [ -n "$PM_FLAG" ] && [ "$MODE" != "build-binary" ]; then
    die_usage "--$PM_FLAG can only be used with --build-binary (--package installs dependencies itself)."
fi

# ------------------------------------------------------------------
# Dispatch
# ------------------------------------------------------------------

case "$MODE" in
    build-binary)    build_binary ;;
    package)
        case "$FORMAT" in
            deb) package_debian ;;
            pkg) package_arch ;;
            rpm) package_fedora ;;
        esac
        ;;
esac
