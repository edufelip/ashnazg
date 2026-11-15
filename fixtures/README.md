# Android framework fixtures

This is a standalone Android Gradle build for real framework APKs used by the
Ashnazg host and CI smoke harness. It does not depend on `agent-android` or an
Expo account/service.

Install the pinned React Native dependencies, then build both debug APKs from
this directory:

```sh
cd reactnative && npm ci --no-audit --no-fund
cd ..
./gradlew :compose:assembleDebug :reactnative:assembleDebug
```

The React Native app plugin bundles JavaScript with Metro and builds React
Native's New Architecture `appmodules` library and Hermes into the APK. It pins
React Native 0.81.5, React 19.1.0, and matching Metro/Babel/CLI packages. This
uses Gradle 8.11.1 and Android Gradle Plugin 8.9.1; building the React Native APK
also requires Android NDK 27.1.12297006 and CMake 3.22.1.

| Framework | APK | Application ID / launcher activity | Native semantics |
| --- | --- | --- | --- |
| Compose | `compose/build/outputs/apk/debug/compose-debug.apk` | `com.ashnazg.fixture.compose` / `com.ashnazg.fixture.compose.MainActivity` | `compose-button` starts as `Compose count: 0`; tap changes it to `Compose count: 1`. Editable `compose-input`. |
| React Native | `reactnative/build/outputs/apk/debug/reactnative-debug.apk` | `com.ashnazg.fixture.reactnative` / `com.ashnazg.fixture.reactnative.MainActivity` | `rn-button` starts as `React Native count: 0`; tap changes it to `React Native count: 1`. `rn-input` is a native `TextInput` with a Unicode placeholder. |

Compose exports test tags as Android resource IDs using
`testTagsAsResourceId = true`. React Native uses actual `testID` props on a
native `Pressable` and `TextInput`; the button also declares the accessible
button role and a changing accessible label.
