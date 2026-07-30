"""Mounts — remote storage mounted as local paths via rclone.

A mount is a named remote-storage mount: an rclone remote spec ("gdrive:" or
"s3remote:bucket/prefix") plus a mountpoint under home_dir()/mounts/<name>.
Once mounted, the path flows through /api/fs/* and every reader untouched —
the app itself still only ever sees local absolute paths (D2/D3 reframed:
remoteness lives in the mount layer). Credentials live exclusively in
rclone's own config; this module stores none.

Mount lifecycle goes through `rclone rcd`, rclone's remote-control daemon,
over its local HTTP API (mount/mount, mount/unmount, mount/listmounts) —
one cross-platform mount API instead of per-OS umount commands. The daemon
is spawned with its {port, pid} recorded in home_dir()/rcd.json and reused
across server runs (the spawn-or-reuse pattern of the tile-server daemons,
templates/geotiff/tile_server.py). It requires basic auth with a random
per-daemon secret, recorded alongside port/pid — see the _rcd_auth block;
loopback is not a boundary against the browser. Unmount is an explicit user
action.

Whether the daemon (and its mounts) survives the server dying depends on
FUSED_RENDER_RCLONE_PERSIST (see _rclone_should_persist). In DEV (dev.sh sets
it) rcd is spawned detached (setsid) so it deliberately SURVIVES the frequent
watchfiles restarts — a fresh server re-adopts the live mounts via
mount/listmounts instead of re-mounting + re-warming the VFS cache. In
PRODUCTION (unset) rcd is a normal child that dies with the server, so quitting
the app tears the mounts down cleanly; the next launch finds the dead pid in
rcd.json stale and respawns.

Store: home_dir()/mounts.json, whole-file last-write-wins like
shell/bookmarks.py. Same acyclic-router + X-Fused-guard conventions.

This package is organized by concern — config, store, rcd (daemon + rc
client), access (rc-mediated stat/list), signing, direct_listing, probe
(direct S3/GCS), lifecycle, credentials, automount, health, endpoints — but
`fused_render.shell.mounts` remains the single public surface: every name
below is re-exported here exactly as it was when this was one file, so
`import fused_render.shell.mounts as mounts_mod` and
`from fused_render.shell import mounts` are unaffected by the split.
"""
import base64
import collections
import configparser
import email.utils
import errno
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import stat as stat_mod
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ElementTree
from datetime import datetime

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.shell import gcssign, s3sign, storage

# The stdlib/project imports above are not used directly in this file — they
# are re-imported here (matching the original single-file module's top-level
# import list) so `mounts_mod.subprocess`, `mounts_mod.storage`, etc. keep
# resolving for any caller that reaches through the package this way (tests
# monkeypatch e.g. `mounts_mod.subprocess.Popen` to intercept a submodule's
# own `import subprocess` — the same cached module object, so the patch
# applies regardless of which submodule actually calls it).

logger = logging.getLogger(__name__)

from .config import (
    NFS_MOUNT_OPT,
    SERVE_VFS_OPT,
    VFS_OPT,
    _VFS_OPT_TO_SERVE_PARAM,
    _effective_serve_read_only,
    _nfs_mount_opt,
    _require_fused,
    _serve_params,
    _serve_vfs_opt_for,
    _vfs_opt_for,
)
from .store import (
    _IO_REPARSE_TAG_MOUNT_POINT,
    _ismount,
    _mounts_generation,
    _path,
    _store_lock,
    _update_mount,
    _write,
    add_mount,
    ensure_mounts_dir,
    get_mount,
    list_mounts,
    mountpoint,
    mounts_dir,
    remove_mount,
)
from .rcd import (
    RCD_LOG_MAX_BYTES,
    WINFSP_DOWNLOAD_URL,
    _DEAD_PORT_TTL_S,
    _KILL_TIMEOUT_S,
    _LIVE_PORT_TTL_S,
    _RCD_RC_USER,
    _RC_JOB_POLL_S,
    _confirmed_our_rcd,
    _copytruncate_rcd_log,
    _ensure_rcd_locked,
    _kill_current_rcd,
    _live_port_cache,
    _live_port_lock,
    _live_rcd_port,
    _pid_alive,
    _pid_looks_like_rcd,
    _rc,
    _rc_cancellable,
    _rcd_auth,
    _rcd_child_env,
    _rcd_lock,
    _rcd_log_path,
    _rcd_registry_path,
    _rcd_state_path,
    _rcd_is_ours_to_reap,
    _rclone_should_persist,
    _register_rcd,
    _rotate_rcd_log,
    _winfsp_available,
    _winfsp_missing_error,
    ensure_rcd,
    mounted_paths,
    rcd_mount_map,
    rclone_bin,
    reap_stale_rcd,
    stop_local_rcd,
    write_rcd_state,
)
from .access import (
    RC_LIST_TIMEOUT_S,
    RC_STAT_TIMEOUT_S,
    RcListError,
    RcListTimeout,
    RcListUnavailable,
    _DIRECT_PROBE_MIN_S,
    _GATE_READ_CAP,
    _STAT_INDETERMINATE,
    _detect_read_only,
    _gcs_anonymous,
    _http_serves,
    _rc_stat_item,
    _rc_timed_out,
    _read_only_mountpoints,
    _refresh_read_only_flag,
    _ro_cache,
    _ro_cache_lock,
    _s3_without_credentials,
    _serves_lock,
    _stat_item,
    export_ro_mounts_env,
    is_mount_backed,
    is_mount_root,
    is_mounts_root,
    mount_read_only,
    rc_kind_for,
    rc_list_dir,
    rc_modtime_epoch,
    rc_mtime_for,
    rc_read_bounded,
    rc_stat_for,
    rc_stat_result,
    serve_url_for,
    serves_path,
)
from .signing import (
    _BOTOCORE_CHAIN_TTL_S,
    _CRED_TTL_S,
    _GCS_NAME_CACHES,
    _GCS_TOKEN_SLACK_S,
    _LINK_TTL_S,
    _NAME_CACHES,
    _NO_REDIRECT_OPENER,
    _NoRedirect,
    _SESSION_TOKEN_LINK_TTL_S,
    _SIGN_EXPIRY_S,
    _SIGN_NEG_TTL_S,
    _SIGN_VALIDATE_TIMEOUT_S,
    _UPSTREAM_LINKS_CAP,
    _UPSTREAM_MAPS,
    _adopt_region_on_301,
    _anonymous_s3,
    _botocore_chain,
    _botocore_creds_cache,
    _cache_locks,
    _cached_resolve,
    _cannot_presign,
    _cred_cache,
    _fix_dotted_bucket_url,
    _gcs_bearer_fallback,
    _gcs_bearer_token,
    _gcs_credentialed,
    _gcs_credentials,
    _gcs_creds_cache,
    _gcs_object_url,
    _gcs_public_object_url,
    _gcs_signable,
    _gcs_signed_url,
    _gcs_signer,
    _gcs_signer_cache,
    _gcs_token_cache,
    _invalidate_gcs_creds,
    _invalidate_upstream_caches,
    _link_ttl,
    _mount_for,
    _public_object_url,
    _remote_config,
    _s3_base_url,
    _s3_bucket_prefix_region,
    _s3_get_direct,
    _s3_list_root,
    _s3_object_url,
    _s3_request_url,
    _s3_signable,
    _s3_signable_shape,
    _sign_neg_cache,
    _signable_credentials,
    _signing_region,
    _store_upstream_link,
    _upstream_cfg,
    _upstream_links,
    _upstream_lock,
    _upstream_mode,
    _upstream_region,
    _validation_locks,
)
from .direct_listing import (
    DirectListError,
    GCS_LIST_TIMEOUT_S,
    S3ListError,
    S3_LIST_TIMEOUT_S,
    _GCS_LIST_URL,
    _S3_XMLNS,
    _gcs_bucket_prefix,
    _gcs_get_direct,
    _s3_listing_prefix,
    direct_list_anonymous,
    direct_list_capable,
    direct_list_page,
    gcs_direct_capable,
    gcs_list_page,
    s3_direct_capable,
    s3_list_page,
)
from .probe import (
    DirectHead,
    DirectProbeError,
    _GCS_OBJ_URL,
    _HEAD_TIMEOUT_S,
    _demote_gsign,
    _direct_stat_item,
    _gcs_has_children,
    _gcs_head,
    _gcs_sign_mode_url,
    _gcs_validate_and_sign,
    _http_date_epoch,
    _s3_has_children,
    _s3_head,
    _sign_mode_url,
    _sign_single_flight,
    _sign_validation_status,
    _upstream_url_for,
    _validate_and_sign,
    bearer_upstream_for,
    direct_head,
    direct_is_dir,
    invalidate_gcs_token,
    upstream_url_for,
)
from .lifecycle import (
    DAEMON_STATE_FILES,
    PROBE_TIMEOUT,
    _FORCE_UNMOUNT_WIN32_BUDGET_S,
    _MOUNT_ATTACH_DEADLINE_S,
    _MOUNT_ATTACH_POLL_S,
    _QUIT_RC_UNMOUNT_TIMEOUT_S,
    _QUIT_UNMOUNT_BUDGET_S,
    _UNSET,
    _await_ismount,
    _force_unmount,
    _is_mounted,
    _mount_wedged,
    _quit_tile_daemons,
    _stop_serve_for,
    _sync_serves_locked,
    _unmount_for_quit,
    attach_mount,
    detach_mount,
    mount_restart_reason,
    mount_state,
    mount_view,
    reconnect_mount,
    sync_serves,
    unmount_all_for_quit,
)
from .credentials import (
    CloudUrlError,
    _BAD_CRED_MARKERS,
    _CRED_EXPIRED_MSG,
    _URL_SCHEME_TYPES,
    _aws_profiles,
    _credential_probe,
    _credential_suggestions,
    _detected_credential_error,
    _mount_credential_status,
    _rclone_config_dump,
    _rclone_state,
    _rclone_state_view,
    _remote_label,
    _remote_type,
    _slug,
    _suggestions_view,
    broken_mount_error,
    resolve_cloud_url,
)
from .automount import (
    LEARN_MOUNT_NAME,
    _force_detach_learn_mount,
    ensure_learn_mount,
    learn_mount_ready,
    learn_zip_path,
)
from .health import (
    HEALTH_POLL_INTERVAL,
    _NEEDS_RECONNECT,
    _health_emit,
    _health_episodes,
    _health_event_seq,
    _health_events,
    _health_log_lock,
    _health_loop,
    _health_started,
    _health_thread,
    health_snapshot,
    poll_once,
    restart_rcd,
    run_automount,
    start_health_monitor,
    startup,
)
from .endpoints import (
    create_detected_remote,
    create_mount,
    create_remote,
    delete_mount,
    get_mounts,
    mount_endpoint,
    reconnect_endpoint,
    resolve_url_endpoint,
    restart_endpoint,
    router,
    unmount_endpoint,
)
