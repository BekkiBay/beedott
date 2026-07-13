/*
  MeetMind Popup Script
  Управляет интерфейсом popup окна
*/

document.addEventListener('DOMContentLoaded', () => {
  console.log('[MeetMind Popup] Инициализация');

  const statusIndicator = document.getElementById('statusIndicator');
  const statusText = document.getElementById('statusText');
  const meetingIdElement = document.getElementById('meetingId');
  const connectionStatusElement = document.getElementById('connectionStatus');
  const reconnectBtn = document.getElementById('reconnectBtn');
  const helpBtn = document.getElementById('helpBtn');

  // Функция обновления статуса
  function updateStatus() {
    try {
      chrome.runtime.sendMessage({ action: 'getStatus' }, (response) => {
        if (chrome.runtime.lastError) {
          console.error('[MeetMind Popup] Ошибка получения статуса:', chrome.runtime.lastError);
          updateUIStatus(false, 'Ошибка подключения');
          return;
        }

        if (response && response.error) {
          console.error('[MeetMind Popup] Ошибка сервера:', response.error);
          updateUIStatus(false, 'Ошибка сервера');
          return;
        }

        const connected = response && response.connected;
        const meetingId = response && response.meetingId ? response.meetingId : '-';
        
        updateUIStatus(connected, meetingId);
      });
    } catch (error) {
      console.error('[MeetMind Popup] Ошибка updateStatus:', error);
      updateUIStatus(false, 'Ошибка получения статуса');
    }
  }

  function updateUIStatus(connected, meetingId) {
    // Обновляем индикатор статуса
    const statusDot = statusIndicator.querySelector('.status-dot');
    
    if (connected) {
      statusDot.classList.remove('disconnected');
      statusDot.classList.add('connected');
      statusText.textContent = 'Подключено к MeetMind';
      connectionStatusElement.textContent = 'Активно';
      connectionStatusElement.className = 'success';
      reconnectBtn.disabled = false;
    } else {
      statusDot.classList.remove('connected');
      statusDot.classList.add('disconnected');
      statusText.textContent = 'Демон не запущен';
      connectionStatusElement.textContent = 'Отключено';
      connectionStatusElement.className = 'error';
      reconnectBtn.disabled = false;
    }

    // Обновляем ID встречи
    if (meetingId && meetingId \!== 'unknown' && meetingId \!== '-') {
      meetingIdElement.textContent = meetingId;
    } else {
      meetingIdElement.textContent = 'Не в встречу';
    }
  }

  // Обработчик кнопки переподключения
  reconnectBtn.addEventListener('click', () => {
    console.log('[MeetMind Popup] Переподключение');
    reconnectBtn.disabled = true;
    reconnectBtn.textContent = 'Переподключаюсь...';

    try {
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        const activeTab = tabs[0];
        
        // Отправляем сообщение content script
        chrome.tabs.sendMessage(activeTab.id, { action: 'reconnect' }, () => {
          setTimeout(() => {
            reconnectBtn.textContent = 'Переподключиться';
            updateStatus();
          }, 1500);
        });
      });
    } catch (error) {
      console.error('[MeetMind Popup] Ошибка переподключения:', error);
      reconnectBtn.disabled = false;
      reconnectBtn.textContent = 'Переподключиться';
      updateStatus();
    }
  });

  // Обработчик кнопки помощи
  helpBtn.addEventListener('click', () => {
    console.log('[MeetMind Popup] Помощь');
    alert(`MeetMind - Трансляция событий Google Meet

Требования:
1. Веб-сокет сервер должен работать на ws://localhost:8765
2. Расширение должно быть активно на вкладке meet.google.com
3. Убедитесь что демон запущен перед началом встречи

События которые транслируются:
- speaker: Кто сейчас говорит
- chat: Новые сообщения в чате
- participant_join: Участник присоединился
- participant_leave: Участник покинул
- meeting_start: Встреча началась
- meeting_end: Встреча закончилась

Для справки откройте консоль браузера (F12) чтобы увидеть логи расширения.`);
  });

  // Инициализируем статус
  updateStatus();

  // Обновляем статус каждые 2 секунды
  setInterval(updateStatus, 2000);

  // Прослушиваем сообщения от background script
  chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === 'wsStatusChanged') {
      console.log('[MeetMind Popup] Получено обновление статуса');
      updateStatus();
    }
  });
});
