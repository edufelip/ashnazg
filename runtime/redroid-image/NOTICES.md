# Derived ReDroid image notices

This recipe derives a local image from the immutable Android 15 base
`redroid/redroid@sha256:81889852444606824d33eb0232fa5813bce3f0993afe871785120af0d7c991ad`.
The published index resolves to linux/amd64 manifest
`sha256:25879821f13ffd38d5d6047d832379b96cf72ec520508b45036dca25cdc3737f` and
linux/arm64 manifest
`sha256:b21c57533662275928832602f7c09b83cb198aa9b271135ae49d978857d6795f`.
Registry image config metadata was queried directly for both platform manifests
on 2026-11-15. Both inherit `ENTRYPOINT ["/init", "qemu=1",
"androidboot.hardware=redroid"]` and have no upstream `CMD`; this Dockerfile
leaves `ENTRYPOINT` inherited and adds only boot arguments as `CMD`.

The derived layer adds the Ashnazg Agent and fixture APKs to `/system/app` and
an init rc file that sets `debug.sf.nobootanimation=1`. APKs are built from
this source tree and hashed by the host build orchestrator. Their package and
signing identities remain those of the supplied APK inputs.

Upstream project and documentation: <https://github.com/remote-android/redroid-doc>
Upstream Android device configuration showing the boot-animation property:
<https://github.com/remote-android/device_redroid/blob/redroid-11.0.0/redroid.prop>
Upstream published image: <https://hub.docker.com/r/redroid/redroid>

ReDroid, Android, and included upstream components retain their respective
copyrights and licenses. This notice preserves source and attribution pointers;
it is not a license audit or a grant of redistribution rights. The maintainer
HWC prebuilt `6248d6a` still has unresolved source and license provenance. This
recipe makes no clearance claim for redistribution of the upstream base or
derived image. Review upstream notices and licenses before distributing it.

The 720x1280, 320 dpi settings and disabled boot animation are recipe inputs,
not device boot acceptance. No governor claim is made: original 1 Hz/60 Hz
behavior requires a maintained HAL and measurement and is not implemented by
this image recipe.
