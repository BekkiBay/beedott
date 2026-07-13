/*
  MeetMind Content Script
  Инжектируется на meet.google.com и стримит события в WebSocket сервер
*/

class WSClient {
  constructor(url = 'ws://localhost:8765') {
    this.url = url;
    this.ws = null;
    this.isConnected = false;
    this.reconnectInterval = 3000;
    this.messageQueue = [];
    this.reconnectTimer = null;
    
    console.log('[MeetMind] WSClient инициализирован для', url);
    this.connect();
  }

  connect() {
    try {
      this.ws = new WebSocket(this.url);
      
      this.ws.onopen = () => {
        console.log('[MeetMind] WebSocket подключено');
        this.isConnected = true;
        this.flushQueue();
        this.notifyStatus(true);
        
        // Перезагружаем наблюдатели если нужно
        if (window.__meetmindObservers) {
          console.log('[MeetMind] Возобновляем наблюдатели после переподключения');
        }
      };

      this.ws.onmessage = (event) => {
        console.log('[MeetMind] Сообщение от сервера:', event.data);
      };

      this.ws.onerror = (error) => {
        console.error('[MeetMind] WebSocket ошибка:', error);
      };

      this.ws.onclose = () => {
        console.log('[MeetMind] WebSocket отключено');
        this.isConnected = false;
        this.notifyStatus(false);
        this.scheduleReconnect();
      };
    } catch (error) {
      console.error('[MeetMind] Ошибка при подключении:', error);
      this.scheduleReconnect();
    }
  }

  scheduleReconnect() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    
    this.reconnectTimer = setTimeout(() => {
      console.log('[MeetMind] Попытка переподключения...');
      this.connect();
    }, this.reconnectInterval);
  }

  flushQueue() {
    if (this.messageQueue.length === 0) return;
    
    console.log('[MeetMind] Отправляю очередь из', this.messageQueue.length, 'сообщений');
    const queue = [...this.messageQueue];
    this.messageQueue = [];
    
    queue.forEach(msg => this.send(msg, true));
  }

  send(event, isFromQueue = false) {
    if (\!event.ts) {
      event.ts = new Date().toISOString();
    }
    
    if (\!event.meeting_id) {
      event.meeting_id = this.getMeetingId();
    }

    const message = JSON.stringify(event);

    if (\!this.isConnected) {
      if (\!isFromQueue) {
        console.log('[MeetMind] WS не подключено, добавляю в очередь:', event.type);
        this.messageQueue.push(event);
      }
      return;
    }

    try {
      this.ws.send(message);
      console.log('[MeetMind] Отправлено событие:', event.type);
    } catch (error) {
      console.error('[MeetMind] Ошибка отправки:', error);
      this.messageQueue.push(event);
    }
  }

  getMeetingId() {
    // Извлекаем meeting_id из URL: meet.google.com/xxx-xxxx-xxx
    const match = window.location.pathname.match(/\/([a-z\-]+)/);
    return match ? match[1] : 'unknown';
  }

  notifyStatus(connected) {
    chrome.runtime.sendMessage({
      action: 'wsStatusChanged',
      connected: connected
    }).catch(err => {
      // Игнорируем ошибки если background script ещё не готов
      if (\!err.message.includes('Receiving end does not exist')) {
        console.error('[MeetMind] Ошибка отправки статуса:', err);
      }
    });
  }

  reconnect() {
    console.log('[MeetMind] Принудительное переподключение');
    if (this.ws) {
      this.ws.close();
    }
    this.connect();
  }
}

class SpeakerObserver {
  constructor(wsClient) {
    this.wsClient = wsClient;
    this.currentSpeaker = null;
    this.lastSpeakerTime = 0;
    this.speakerDeduplicationMs = 2000;
    this.observer = null;
    
    console.log('[MeetMind] SpeakerObserver инициализирован');
    this.start();
  }

  start() {
    // Ждём полной загрузки интерфейса
    this.waitForMeetInterface(() => {
      this.setupObserver();
      console.log('[MeetMind] SpeakerObserver запущен');
    });
  }

  waitForMeetInterface(callback) {
    const checkInterval = setInterval(() => {
      if (document.querySelector('[data-requested-participant-id], .KF4T6b, [jsname="HiaYvf"]')) {
        clearInterval(checkInterval);
        // Дополнительная задержка для полной загрузки
        setTimeout(callback, 500);
      }
    }, 100);

    // Таймаут безопасности - 10 секунд
    setTimeout(() => {
      clearInterval(checkInterval);
      if (callback) callback();
    }, 10000);
  }

  setupObserver() {
    // Создаём MutationObserver для отслеживания изменений
    this.observer = new MutationObserver(() => {
      this.checkActiveSpeaker();
    });

    // Слушаем изменения на body и во всём видео контейнере
    const bodyTarget = document.body;
    const videoContainer = document.querySelector('[data-requested-participant-id]') || document.body;

    this.observer.observe(bodyTarget, {
      attributes: true,
      attributeFilter: ['data-requested-participant-id', 'aria-label', 'class'],
      subtree: true,
      characterData: false
    });

    // Первоначальная проверка
    this.checkActiveSpeaker();
  }

  checkActiveSpeaker() {
    try {
      const now = Date.now();
      
      // Пытаемся найти активного спикера через различные селекторы
      let speaker = null;
      let name = null;

      // Стратегия 1: Ищем элемент с активным классом или aria-label
      const activeElement = document.querySelector('[data-requested-participant-id][aria-label]');
      if (activeElement) {
        name = this.extractNameFromElement(activeElement);
      }

      // Стратегия 2: Ищем видео элемент основного спикера
      if (\!name) {
        const videoElement = document.querySelector('video[data-participant-id]');
        if (videoElement) {
          const container = videoElement.closest('[data-requested-participant-id]');
          if (container) {
            name = this.extractNameFromElement(container);
          }
        }
      }

      // Стратегия 3: Ищем через aria-label атрибуты
      if (\!name) {
        const ariaElements = document.querySelectorAll('[aria-label*="is speaking"], [aria-label*="говор"]');
        if (ariaElements.length > 0) {
          name = this.extractNameFromElement(ariaElements[0]);
        }
      }

      speaker = name || 'Unknown';

      // Дедупликация - не отправляем одного спикера чаще чем раз в 2 секунды
      if (speaker \!== this.currentSpeaker && (now - this.lastSpeakerTime) > this.speakerDeduplicationMs) {
        this.currentSpeaker = speaker;
        this.lastSpeakerTime = now;

        console.log('[MeetMind] Активный спикер:', speaker);
        this.wsClient.send({
          type: 'speaker',
          name: speaker,
          ts: new Date().toISOString()
        });
      }
    } catch (error) {
      console.error('[MeetMind] Ошибка при проверке спикера:', error);
    }
  }

  extractNameFromElement(element) {
    if (\!element) return null;

    // Извлекаем из aria-label
    const ariaLabel = element.getAttribute('aria-label');
    if (ariaLabel) {
      // Формат: "Иван Сидоров, is speaking" или "Иван Сидоров, говорит"
      const match = ariaLabel.match(/^([^,]+)/);
      if (match) return match[1].trim();
    }

    // Извлекаем из data-participant-id
    const participantId = element.getAttribute('data-requested-participant-id');
    if (participantId) {
      // Ищем соответствующий элемент с именем
      const nameElement = element.querySelector('[class*="name"], [data-participant-name]');
      if (nameElement) {
        return nameElement.textContent.trim();
      }
    }

    // Последний вариант - берём текст из элемента
    const text = element.textContent.trim().split('\n')[0];
    return text && text.length > 0 && text.length < 100 ? text : null;
  }

  stop() {
    if (this.observer) {
      this.observer.disconnect();
      this.observer = null;
    }
  }
}

class ChatObserver {
  constructor(wsClient) {
    this.wsClient = wsClient;
    this.observedMessages = new Set();
    this.observer = null;
    
    console.log('[MeetMind] ChatObserver инициализирован');
    this.start();
  }

  start() {
    // Ждём загрузки чат-панели
    this.waitForChat(() => {
      this.setupObserver();
      console.log('[MeetMind] ChatObserver запущен');
    });
  }

  waitForChat(callback) {
    const checkInterval = setInterval(() => {
      if (document.querySelector('[jsname="xySENc"], [role="region"][aria-label*="chat"], [data-chat-message]')) {
        clearInterval(checkInterval);
        setTimeout(callback, 500);
      }
    }, 100);

    setTimeout(() => {
      clearInterval(checkInterval);
      callback();
    }, 15000);
  }

  setupObserver() {
    const chatContainer = document.querySelector('[jsname="xySENc"]') || 
                         document.querySelector('[role="region"][aria-label*="chat"]') ||
                         document.querySelector('[data-chat-message]')?.parentElement;

    if (\!chatContainer) {
      console.log('[MeetMind] Чат-панель не найдена');
      return;
    }

    this.observer = new MutationObserver((mutations) => {
      mutations.forEach((mutation) => {
        if (mutation.addedNodes.length > 0) {
          mutation.addedNodes.forEach((node) => {
            if (node.nodeType === Node.ELEMENT_NODE) {
              this.processNewMessage(node);
            }
          });
        }
      });
    });

    this.observer.observe(chatContainer, {
      childList: true,
      subtree: true
    });
  }

  processNewMessage(element) {
    try {
      // Ищем элементы сообщений
      const messageElements = element.querySelectorAll('[data-chat-message], [role="article"]');
      
      messageElements.forEach((msgElement) => {
        const msgId = msgElement.getAttribute('data-message-id') || msgElement.textContent;
        
        if (this.observedMessages.has(msgId)) {
          return; // Уже обработали это сообщение
        }

        const authorElement = msgElement.querySelector('[data-sender-name], [class*="author"], [class*="name"]');
        const textElement = msgElement.querySelector('[data-message-text], [class*="message"], p, span');

        const author = authorElement ? authorElement.textContent.trim() : 'Unknown';
        const text = textElement ? textElement.textContent.trim() : '';

        if (text && text.length > 0) {
          this.observedMessages.add(msgId);

          console.log('[MeetMind] Новое сообщение в чате:', { author, text });
          this.wsClient.send({
            type: 'chat',
            name: author,
            text: text,
            ts: new Date().toISOString()
          });
        }
      });
    } catch (error) {
      console.error('[MeetMind] Ошибка при обработке сообщения чата:', error);
    }
  }

  stop() {
    if (this.observer) {
      this.observer.disconnect();
      this.observer = null;
    }
  }
}

class ParticipantObserver {
  constructor(wsClient) {
    this.wsClient = wsClient;
    this.currentParticipants = new Set();
    this.observer = null;
    
    console.log('[MeetMind] ParticipantObserver инициализирован');
    this.start();
  }

  start() {
    this.waitForParticipantList(() => {
      this.recordInitialParticipants();
      this.setupObserver();
      console.log('[MeetMind] ParticipantObserver запущен');
    });
  }

  waitForParticipantList(callback) {
    const checkInterval = setInterval(() => {
      const participantElements = document.querySelectorAll('[data-requested-participant-id], [data-participant-id]');
      if (participantElements.length > 0) {
        clearInterval(checkInterval);
        setTimeout(callback, 1000);
      }
    }, 100);

    setTimeout(() => {
      clearInterval(checkInterval);
      callback();
    }, 15000);
  }

  recordInitialParticipants() {
    const participantElements = document.querySelectorAll('[data-requested-participant-id], [data-participant-id]');
    participantElements.forEach((element) => {
      const name = this.extractParticipantName(element);
      if (name) {
        this.currentParticipants.add(name);
      }
    });

    console.log('[MeetMind] Начальные участники:', Array.from(this.currentParticipants));
  }

  setupObserver() {
    this.observer = new MutationObserver(() => {
      this.checkParticipantChanges();
    });

    const participantContainer = document.querySelector('[role="main"]') || document.body;
    
    this.observer.observe(participantContainer, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['data-requested-participant-id', 'data-participant-id']
    });
  }

  checkParticipantChanges() {
    try {
      const currentElements = document.querySelectorAll('[data-requested-participant-id], [data-participant-id]');
      const newParticipants = new Set();

      currentElements.forEach((element) => {
        const name = this.extractParticipantName(element);
        if (name) {
          newParticipants.add(name);

          // Проверяем, новый ли это участник
          if (\!this.currentParticipants.has(name)) {
            console.log('[MeetMind] Участник присоединился:', name);
            this.wsClient.send({
              type: 'participant_join',
              name: name,
              ts: new Date().toISOString()
            });
          }
        }
      });

      // Проверяем кто вышел
      this.currentParticipants.forEach((name) => {
        if (\!newParticipants.has(name)) {
          console.log('[MeetMind] Участник покинул:', name);
          this.wsClient.send({
            type: 'participant_leave',
            name: name,
            ts: new Date().toISOString()
          });
        }
      });

      this.currentParticipants = newParticipants;
    } catch (error) {
      console.error('[MeetMind] Ошибка при проверке участников:', error);
    }
  }

  extractParticipantName(element) {
    // Пытаемся извлечь имя участника
    const ariaLabel = element.getAttribute('aria-label');
    if (ariaLabel) {
      const match = ariaLabel.match(/^([^,]+)/);
      if (match) return match[1].trim();
    }

    const nameElement = element.querySelector('[class*="name"], [data-participant-name]');
    if (nameElement) {
      return nameElement.textContent.trim();
    }

    const text = element.textContent.trim().split('\n')[0];
    return text && text.length > 0 && text.length < 100 ? text : null;
  }

  stop() {
    if (this.observer) {
      this.observer.disconnect();
      this.observer = null;
    }
  }
}

class MeetingLifecycle {
  constructor(wsClient) {
    this.wsClient = wsClient;
    
    console.log('[MeetMind] MeetingLifecycle инициализирован');
    this.setupListeners();
    this.notifyMeetingStart();
  }

  setupListeners() {
    // Отправляем событие конца встречи при выходе со страницы
    window.addEventListener('beforeunload', () => {
      this.notifyMeetingEnd();
    });

    // Также отправляем при навигации
    window.addEventListener('pagehide', () => {
      this.notifyMeetingEnd();
    });
  }

  notifyMeetingStart() {
    console.log('[MeetMind] Встреча началась');
    this.wsClient.send({
      type: 'meeting_start',
      ts: new Date().toISOString()
    });
  }

  notifyMeetingEnd() {
    console.log('[MeetMind] Встреча закончилась');
    try {
      this.wsClient.send({
        type: 'meeting_end',
        ts: new Date().toISOString()
      });
    } catch (error) {
      console.error('[MeetMind] Ошибка при отправке meeting_end:', error);
    }
  }
}

// Инициализация при загрузке страницы
function initMeetMind() {
  try {
    console.log('[MeetMind] Инициализация расширения на meet.google.com');

    // Проверяем что мы на meet.google.com
    if (\!window.location.hostname.includes('meet.google.com')) {
      console.log('[MeetMind] Не на meet.google.com, выходим');
      return;
    }

    // Инициализируем WebSocket клиент
    const wsClient = new WSClient('ws://localhost:8765');

    // Сохраняем в глобальную переменную для отладки
    window.__meetmindClient = wsClient;

    // Инициализируем наблюдатели с задержкой чтобы дать интерфейсу загрузиться
    setTimeout(() => {
      try {
        const speakerObserver = new SpeakerObserver(wsClient);
        const chatObserver = new ChatObserver(wsClient);
        const participantObserver = new ParticipantObserver(wsClient);
        const lifecycle = new MeetingLifecycle(wsClient);

        window.__meetmindObservers = {
          speaker: speakerObserver,
          chat: chatObserver,
          participants: participantObserver,
          lifecycle: lifecycle
        };

        console.log('[MeetMind] Все наблюдатели инициализированы');
      } catch (error) {
        console.error('[MeetMind] Ошибка инициализации наблюдателей:', error);
      }
    }, 3000);

  } catch (error) {
    console.error('[MeetMind] Критическая ошибка инициализации:', error);
  }
}

// Запускаем при загрузке страницы
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initMeetMind);
} else {
  initMeetMind();
}

// Также пытаемся инициализировать при загрузке окна
window.addEventListener('load', () => {
  if (\!window.__meetmindClient) {
    console.log('[MeetMind] Повторная инициализация при window.load');
    setTimeout(initMeetMind, 1000);
  }
});
