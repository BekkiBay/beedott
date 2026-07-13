/*
  MeetMind Background Script
  Управляет состоянием расширения и коммуникацией с content scripts
*/

// Состояние WebSocket соединения по вкладкам
const tabStatus = {};

// Слушаем сообщения от content scripts
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  try {
    if (request.action === 'wsStatusChanged') {
      const tabId = sender.tab.id;
      const meetingId = extractMeetingId(sender.url);
      
      console.log('[MeetMind] WS статус изменился для вкладки', tabId, ':', request.connected);
      
      tabStatus[tabId] = {
        connected: request.connected,
        meetingId: meetingId,
        timestamp: new Date().toISOString(),
        url: sender.url
      };

      // Обновляем значок расширения
      updateBadge(tabId, request.connected);

      // Отправляем подтверждение
      sendResponse({ received: true });
    }
  } catch (error) {
    console.error('[MeetMind] Ошибка обработки сообщения:', error);
  }
});

// Слушаем обновления вкладок
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  try {
    if (changeInfo.status === 'complete' && tab.url && tab.url.includes('meet.google.com')) {
      console.log('[MeetMind] Вкладка с Meet загрузилась:', tabId);
      
      // Сбрасываем статус для новой вкладки
      tabStatus[tabId] = {
        connected: false,
        meetingId: extractMeetingId(tab.url),
        timestamp: new Date().toISOString(),
        url: tab.url
      };

      updateBadge(tabId, false);
    }
  } catch (error) {
    console.error('[MeetMind] Ошибка обновления вкладки:', error);
  }
});

// Слушаем закрытие вкладок
chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabStatus[tabId]) {
    console.log('[MeetMind] Вкладка закрыта:', tabId);
    delete tabStatus[tabId];
  }
});

// Обновляем значок расширения в зависимости от статуса
function updateBadge(tabId, connected) {
  try {
    if (connected) {
      chrome.browserAction.setBadgeBackgroundColor({ color: '#4CAF50', tabId: tabId });
      chrome.browserAction.setBadgeText({ text: '✓', tabId: tabId });
    } else {
      chrome.browserAction.setBadgeBackgroundColor({ color: '#f44336', tabId: tabId });
      chrome.browserAction.setBadgeText({ text: '✗', tabId: tabId });
    }
  } catch (error) {
    console.error('[MeetMind] Ошибка обновления значка:', error);
  }
}

// Извлекаем meeting ID из URL
function extractMeetingId(url) {
  try {
    const match = url.match(/meet\.google\.com\/([a-z\-]+)/);
    return match ? match[1] : 'unknown';
  } catch (error) {
    return 'unknown';
  }
}

// API для popup.js - получить текущий статус
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'getStatus') {
    try {
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        const activeTab = tabs[0];
        const status = tabStatus[activeTab.id] || {
          connected: false,
          meetingId: 'unknown',
          url: activeTab.url
        };
        sendResponse(status);
      });
      return true; // Укажи что ответ будет асинхронным
    } catch (error) {
      console.error('[MeetMind] Ошибка получения статуса:', error);
      sendResponse({ error: error.message });
    }
  }
});

// API для popup.js - переподключиться
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'reconnect') {
    try {
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        const activeTab = tabs[0];
        
        // Отправляем сообщение content script чтобы переподключиться
        chrome.tabs.sendMessage(activeTab.id, { action: 'reconnect' }, () => {
          sendResponse({ success: true });
        });
      });
      return true;
    } catch (error) {
      console.error('[MeetMind] Ошибка переподключения:', error);
      sendResponse({ error: error.message });
    }
  }
});

console.log('[MeetMind] Background script загружен');
