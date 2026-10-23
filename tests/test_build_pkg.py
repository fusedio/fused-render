"""Structural guards on the macOS network installer (scripts/build_pkg.sh,
scripts/pkg/) and its wiring into release.yml.

Like test_build_dmg_diagnostics.py: pkgbuild/productbuild/Installer only
exist on macOS and the install itself needs a real Mac, so nothing here runs
the installer. What CAN be checked from anywhere is the contract between the
pieces — the facts build_pkg.sh writes are the facts the postinstall reads,
every template placeholder is filled, the hash is checked before the image
is mounted, the workflow builds the pkg from the stapled DMG under the same
URL it uploads it to and while the signing keychain still exists — because a
break in any of those is invisible until a user's install fails.
"""
import json
import os
import re
import subprocess
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_REPO, "scripts")
_BUILD_PKG = os.path.join(_SCRIPTS, "build_pkg.sh")
_PKG_DIR = os.path.join(_SCRIPTS, "pkg")
_POSTINSTALL = os.path.join(_PKG_DIR, "postinstall")
_WORKFLOW = os.path.join(_REPO, ".github", "workflows", "release.yml")
_WRITE_MANIFEST = os.path.join(_SCRIPTS, "download_page", "write_manifest.py")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _bash():
    for candidate in ("/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash"):
        if os.path.exists(candidate):
            return candidate
    pytest.skip("no bash to syntax-check with")


@pytest.mark.parametrize("script", [_BUILD_PKG, _POSTINSTALL])
def test_scripts_parse(script):
    subprocess.run([_bash(), "-n", script], check=True)


def test_scripts_are_executable():
    for script in (_BUILD_PKG, _POSTINSTALL):
        assert os.access(script, os.X_OK), f"{os.path.basename(script)} must be executable (pkgbuild copies the mode into the pkg)"


def test_postinstall_fails_closed():
    src = _read(_POSTINSTALL)
    assert src.startswith("#!/bin/bash\n"), "Installer runs scripts via their shebang; /bin/bash is the one every Mac has"
    assert "set -euo pipefail" in src
    assert "--insecure" not in src and " -k " not in src, "TLS verification is part of the pin"


def test_postinstall_verifies_the_hash_before_mounting():
    """The sha256 pinned at build time is the only thing standing between a
    compromised CDN and root-run `ditto` of whatever it served; the image
    must not be attached (nor anything copied) before that check passes."""
    src = _read(_POSTINSTALL)
    verify = src.index("shasum -a 256")
    mount = src.index("hdiutil attach")
    copy = src.index("ditto")
    assert verify < mount < copy
    assert 'if [[ "$ACTUAL_SHA256" != "$DMG_SHA256" ]]' in src
    assert "fail " in src[verify:mount]


def test_release_env_contract():
    """build_pkg.sh writes release.env; the postinstall sources it. Every
    variable the postinstall uses from it must be written, under that name."""
    build = _read(_BUILD_PKG)
    post = _read(_POSTINSTALL)
    written = set(re.findall(r"(?m)^([A-Z0-9_]+)='\$\{[A-Z0-9_]+\}'$", build))
    assert written == {"APP_NAME", "DISPLAY_NAME", "VERSION", "ARCH", "DMG_URL", "DMG_SHA256"}
    assert 'source "$HERE/release.env"' in post
    for var in ("APP_NAME", "VERSION", "ARCH", "DMG_URL", "DMG_SHA256"):
        assert f"${var}" in post or f"${{{var}" in post, f"postinstall never reads {var}"


def test_every_template_placeholder_is_filled():
    build = _read(_BUILD_PKG)
    filled = set(re.findall(r"s\|(@[A-Z_]+@)\|", build))
    for name in ("distribution.xml", "welcome.html", "conclusion.html"):
        used = set(re.findall(r"@[A-Z_]+@", _read(os.path.join(_PKG_DIR, name))))
        assert used, f"{name} has no placeholders — did the templating move?"
        assert used <= filled, f"{name} uses placeholders build_pkg.sh never fills: {sorted(used - filled)}"
    # And the build refuses to ship a page with one left over.
    assert "grep -q '@[A-Z0-9_]*@'" in build


def test_distribution_is_well_formed_after_filling(tmp_path):
    """Run build_pkg.sh's own `fill` on the real template with dummy values:
    the XML must parse and the arch check must key off the filled arch."""
    import xml.etree.ElementTree as ET

    src = _read(os.path.join(_PKG_DIR, "distribution.xml"))
    values = {
        "@APP_NAME@": "FusedRender", "@DISPLAY_NAME@": "Fused Render", "@VERSION@": "0.0.1",
        "@ARCH@": "arm64", "@PKG_ID@": "io.fused.render.installer",
        "@COMPONENT_PKG@": "FusedRender-component.pkg", "@DMG_SIZE_MB@": "123",
    }
    for k, v in values.items():
        src = src.replace(k, v)
    root = ET.fromstring(src)
    assert root.tag == "installer-gui-script"
    assert root.find("options").get("rootVolumeOnly") == "true"
    assert root.find("options").get("require-scripts") == "true"
    assert root.find("allowed-os-versions/os-version").get("min") == "11.0"
    assert root.find("installation-check").get("script") == "checkMachine()"
    assert "'arm64' == 'arm64'" in root.find("script").text
    ref = root.find("pkg-ref")
    assert ref.get("id") == "io.fused.render.installer"
    assert ref.text == "FusedRender-component.pkg"
    assert root.find("choice[@id='io.fused.render.installer']/pkg-ref").get("id") == "io.fused.render.installer"


def test_build_pkg_pins_the_file_it_will_download():
    build = _read(_BUILD_PKG)
    assert "--nopayload" in build, "a payload would re-enable Installer's bundle relocation"
    assert 'basename "$DMG_URL")" != "$(basename "$DMG_PATH")' in build, "the URL must name the hashed file"
    assert "https://*)" in build


def test_build_pkg_is_macos_bash32_safe():
    """macOS's /bin/bash is 3.2: `"${arr[@]}"` on an empty array aborts under
    set -u there, which is why the keychain option is a scalar."""
    build = _read(_BUILD_PKG)
    assert "${KC_OPT[@]}" not in build
    assert "KC_OPT=()" not in build


def test_workflow_builds_the_pkg_from_the_final_dmg_under_the_published_url():
    wf = _read(_WORKFLOW)
    render_job = wf[wf.index("  build-sign-notarize-release:"):wf.index("  build-sign-notarize-release-intel:")]
    intel_job = wf[wf.index("  build-sign-notarize-release-intel:"):wf.index("  build-sign-notarize-release-bot:")]
    for job in (render_job, intel_job):
        dmg_step = job.index("Build, sign, notarize, staple DMG")
        pkg_step = job.index("Build, sign, notarize, staple installer pkg")
        keychain_gone = job.index("Delete ephemeral keychain")
        upload = job.index("aws s3 cp \"$DMG_PATH\"")
        # After the DMG is final (stapled), before the keychain that holds the
        # installer identity + notary profile is deleted, before the upload.
        assert dmg_step < pkg_step < keychain_gone < upload
        # The pkg pins the DMG under exactly the key the upload publishes.
        assert 'build_pkg.sh "$DMG_PATH" "https://${CDN_DOMAIN}/fused-render-dmgs/$(basename "$DMG_PATH")"' in job
        assert 's3://${S3_BUCKET}/fused-render-dmgs/$(basename "$DMG_PATH")' in job
        # Only a signed pkg is published.
        assert 'pkgutil --check-signature "$PKG_PATH" | grep -q "Status: signed"' in job
        # The installer cert is imported before the partition list is set.
        assert job.index("-T /usr/bin/productbuild") < job.index("set-key-partition-list")
    # Only the render job feeds the download page.
    assert "pkg_url: ${{ steps.artifacts.outputs.pkg_url }}" in render_job
    assert "PKG_URL: ${{ needs.build-sign-notarize-release.outputs.pkg_url }}" in wf


def test_write_manifest_carries_pkg_url(tmp_path):
    env = dict(os.environ, VERSION="1.2.3", DMG_URL="https://cdn/x.dmg",
               PKG_URL="https://cdn/x.pkg")
    out = subprocess.run([sys.executable, "-I", _WRITE_MANIFEST], env=env,
                         capture_output=True, text=True, check=True).stdout
    manifest = json.loads(out)
    assert manifest["pkg_url"] == "https://cdn/x.pkg"
    # Unset → carried forward from the existing manifest, like the others.
    existing = tmp_path / "latest.json"
    existing.write_text(json.dumps({"version": "1.2.2", "pkg_url": "https://cdn/old.pkg"}))
    env = dict(os.environ, VERSION="1.2.3", EXISTING_MANIFEST_PATH=str(existing))
    env.pop("PKG_URL", None)
    out = subprocess.run([sys.executable, "-I", _WRITE_MANIFEST], env=env,
                         capture_output=True, text=True, check=True).stdout
    assert json.loads(out)["pkg_url"] == "https://cdn/old.pkg"


def test_download_page_shows_the_pkg_only_when_the_manifest_has_one():
    page = _read(os.path.join(_SCRIPTS, "download_page", "index.html"))
    assert 'id="pkg"' in page and 'id="dl-pkg"' in page
    assert "pkg: m.pkg_url" in page
    assert "if (m.pkg_url)" in page
    # Hidden by default in the markup: nothing to click until a signed one ships.
    assert re.search(r'<a id="pkg"[^>]*\bhidden\b', page)
    assert re.search(r'<p class="dl-spec" id="dl-pkg" hidden>', page)
