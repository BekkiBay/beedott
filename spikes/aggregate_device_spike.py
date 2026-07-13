"""Спайк B: программное создание Aggregate Device через CoreAudio HAL (PyObjC).

Go/no-go: можно ли без Audio MIDI Setup создать aggregate-устройство
(в проде: BlackHole + микрофон; здесь: default-микрофон, т.к. BlackHole
ставится отдельно) и открыть его через sounddevice.

Запуск: uv run python spikes/aggregate_device_spike.py
"""
import time
import uuid

import CoreAudio
import objc
import sounddevice as sd

kAudioObjectSystemObject = 1


def _addr(selector):
    return CoreAudio.AudioObjectPropertyAddress(
        mSelector=selector,
        mScope=CoreAudio.kAudioObjectPropertyScopeGlobal,
        mElement=CoreAudio.kAudioObjectPropertyElementMain,
    )


def default_input_uid() -> str:
    err, _, device_id = CoreAudio.AudioObjectGetPropertyData(
        kAudioObjectSystemObject,
        _addr(CoreAudio.kAudioHardwarePropertyDefaultInputDevice),
        0, None, 4, None,
    )
    assert err == 0, f"default input device: err={err}"
    device_id = int.from_bytes(device_id, "little")
    err, _, uid = CoreAudio.AudioObjectGetPropertyData(
        device_id, _addr(CoreAudio.kAudioDevicePropertyDeviceUID),
        0, None, 8, None,
    )
    assert err == 0, f"device uid: err={err}"
    return objc.pyobjc_id  # placeholder, replaced below


def main():
    # 1. UID default-микрофона
    err, _, data = CoreAudio.AudioObjectGetPropertyData(
        kAudioObjectSystemObject,
        _addr(CoreAudio.kAudioHardwarePropertyDefaultInputDevice),
        0, None, 4, None,
    )
    assert err == 0, f"default input: err={err}"
    mic_id = int.from_bytes(bytes(data), "little")
    print(f"OK default input device id: {mic_id}")

    err, _, uid = CoreAudio.AudioObjectGetPropertyData(
        mic_id, _addr(CoreAudio.kAudioDevicePropertyDeviceUID), 0, None, 16, None
    )
    assert err == 0, f"mic uid: err={err}"
    mic_uid = str(uid)
    print(f"OK mic uid: {mic_uid}")

    # 2. Программное создание Aggregate Device
    desc = {
        CoreAudio.kAudioAggregateDeviceNameKey: "MeetMind In (spike)",
        CoreAudio.kAudioAggregateDeviceUIDKey: f"meetmind-spike-{uuid.uuid4()}",
        CoreAudio.kAudioAggregateDeviceSubDeviceListKey: [
            {CoreAudio.kAudioSubDeviceUIDKey: mic_uid},
            # в проде вторым идёт: {kAudioSubDeviceUIDKey: "BlackHole2ch_UID"}
        ],
    }
    err, agg_id = CoreAudio.AudioHardwareCreateAggregateDevice(desc, None)
    assert err == 0, f"AudioHardwareCreateAggregateDevice: err={err}"
    print(f"OK aggregate device created: {agg_id}")

    try:
        # 3. sounddevice видит и открывает его
        sd._terminate(); sd._initialize()  # обновить список устройств
        devices = sd.query_devices()
        agg = [d for d in devices if "MeetMind In (spike)" in d["name"]]
        assert agg, f"aggregate не виден sounddevice: {[d['name'] for d in devices]}"
        print(f"OK sounddevice видит: {agg[0]['name']} ({agg[0]['max_input_channels']} ch)")

        rec = sd.rec(int(1 * 16000), samplerate=16000,
                     channels=min(2, agg[0]["max_input_channels"]),
                     device=agg[0]["index"])
        sd.wait()
        print(f"OK записано {rec.shape} с aggregate-устройства")
    finally:
        err = CoreAudio.AudioHardwareDestroyAggregateDevice(agg_id)
        print(f"OK destroyed (err={err})")

    print("SPIKE B (API path): GO")


if __name__ == "__main__":
    main()
