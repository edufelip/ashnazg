# Meer Android runtime CI pilot

The protected `develop` run at `83ce2d949ec2d5571086ac1ceecb072b68a61140`
built one shared DEV and AndroidTest APK pair and passed the existing Meer
Login → Search → Home journey on both Ashnazg ReDroid and the Android Studio
AVD. The job wrote its timing and cleanup summary and passed its final gate,
which requires both runtime steps to succeed. The readiness milestones differ
between the two steps. [Protected CI run](https://github.com/edufelip/meer-mono/actions/runs/44352327085)

## Local preflight

The runner from Meer PR #191 at `a4c2f58f2cceb016136254b58f04d5b322cfa30a`
passed the existing Login → Search → Home Detox journey on two freshly booted
API 35 guests. Both ran the same prebuilt Meer DEV and AndroidTest APKs with
location and animations disabled. The runner required the selected test to
pass and cleaned up the services and reverse mappings it created.

| Runtime | Detox command including app setup | Pilot command including services | Result |
| --- | ---: | ---: | --- |
| ReDroid | 75.23 s | 77.82 s | Pass |
| Android Studio AVD | 85.07 s | 93.01 s | Pass |

[ReDroid journey](redroid-journey.json) · [AVD journey](avd-journey.json) ·
[Arm observations and cleanup](arm-observations.json)

Local ReDroid used the pinned Android 15 64-bit-only image and private Binderfs.
The local AVD used the workflow's API 35 SwiftShader
configuration and an already accessible KVM device. These are individual
functional passes. The arm observations record the different readiness and
cleanup boundaries.

The updated workflow uses Meer's existing self-hosted Android runner. Its
pinned Action selects the same 64-bit-only image and creates each guest's
Binder devices through private Binderfs. A local run of that exact Action
commit passed the same Meer journey and removed the owned guest. The workflow
publishes timing receipts in the job summary.

[Private Binderfs Action receipt](private-binder-action.json) ·
[Ashnazg Linux development PR](https://github.com/edufelip/ashnazg/pull/23)

The CI workflow runs on protected refs after reviewed delivery.
[Meer pilot PR](https://github.com/edufelip/meer-mono/pull/191)

For controlled latency and runtime-process memory results, see the
[four-pair benchmark](../meer-runtime-comparison-2026-11-18/README.md).
