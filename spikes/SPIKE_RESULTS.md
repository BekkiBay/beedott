# Результаты спайков M0 (2026-07-13)

Машина: macOS 27.0, Apple Silicon, Xcode 26.5 (swiftc), подписных идентичностей нет
(0 valid identities), BlackHole НЕ установлен.

## Спайк A — Core Audio Process Taps: **GO** ✅

`ProcessTapSpike/main.swift` (компилируется голым `swiftc`, ad-hoc подпись):
pid → `kAudioHardwarePropertyTranslatePIDToProcessObject` → `CATapDescription(
stereoMixdownOfProcesses:)` → `AudioHardwareCreateProcessTap` → приватный aggregate
c `kAudioAggregateDeviceTapListKey` → IOProc → AVAudioFile(WAV).

Прогон: tap на PID `afplay` (30-сек файл), запись 5 сек →
`sr=48000, stereo, rms=0.055, peak=0.47` — **звук процесса захвачен**.

Выводы:
- Работает **headless, без Developer ID, с ad-hoc подписью** на этой машине
  (macOS 27). Платный аккаунт для локального личного использования НЕ нужен.
  Для упакованного .app всё равно фиксируем Bundle/Team ID (personal team) — TCC-стабильность.
- TCC-промпт при headless-прогоне не блокировал запись. На чистой системе ожидаем
  промпт «System Audio Recording» — обрабатывается онбордингом (M6).
- Грабля: tap на PID, который умер (короткоживущий процесс), пишет тишину без
  ошибки → в проде детект должен проверять живость PID и «тишину» первых секунд.
- Грабля: `AVAudioFile` нужно явно `close()` перед `exit()` — иначе WAV-header
  не финализируется (0 фреймов при чтении).

## Спайк B — программный Aggregate Device (fallback-путь): **GO (API)** ✅ / BlackHole — ручной шаг

- PyObjC-биндинги `AudioObjectGetPropertyData` неюзабельны (TypeError на C-массивах) —
  **отказ от PyObjC** для HAL.
- Решение: хелпер `spikes/aggdev.swift` (66KB бинарь, компилируется swiftc за секунды):
  `aggdev create <name> <uid,...>` / `destroy <id>` / `list-inputs`. Python дергает
  его через subprocess.
- Прогон: `aggdev create "MeetMind In (spike)" BuiltInMicrophoneDevice` →
  sounddevice увидел устройство, записал 1 сек с мика (rms 0.005 — живой сигнал,
  mic-TCC headless тоже ок), `destroy` чистый.
- `brew install blackhole-2ch` требует sudo-пароль → **ручной шаг пользователя**
  (или онбординг с privileged helper). После установки sub-device список =
  `BlackHole2ch_UID,BuiltInMicrophoneDevice`.
- Output-сторона (Multi-Output + переключение default output) — тем же API,
  реализуется в M4 Task 4.2, если fallback вообще понадобится.

## Гейт-решение (Task 0.6)

| Путь | Статус | Роль |
|---|---|---|
| A: Process Tap (Swift) | GO, проверен записью | **Primary** — M3/M4 по плану (Swift shell) |
| B: aggdev-хелпер + BlackHole | GO по API; BlackHole ждёт sudo | Постоянный fallback (macOS <14.4 / сбой tap) |

Continuum-режим (без Swift) не нужен: Swift-тулчейн на машине работает, спайк A
прошёл. Идём по основному плану M1 → M6. Бонус: aggdev-паттерн (мелкие swift-хелперы,
компилируемые setup-скриптом без Xcode-проекта) может пригодиться и в M1-M2 до
появления полноценного .app.
