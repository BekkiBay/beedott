import time

import pytest

from daemon.recorder import Recorder


@pytest.mark.hardware  # реальное аудио-устройство + TCC; не входит в дефолтный прогон
def test_streams_to_disk(tmp_path):
    wav = tmp_path / "a.wav"
    r = Recorder(out_path=wav)
    r.start()
    time.sleep(2)
    size_1s = wav.stat().st_size
    time.sleep(2)
    r.stop()
    assert wav.stat().st_size > size_1s > 44  # файл растёт ВО ВРЕМЯ записи
