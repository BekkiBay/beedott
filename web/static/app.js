/**
 * MeetMind Web UI - JavaScript
 * Управление интерфейсом, API и статусом демона
 */

// ============================================================================
// Инициализация
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
    // Настройка marked.js
    setupMarked();

    // Модальное окно добавления встречи
    setupAddMeetingModal();

    // Проверка статуса демона
    updateDaemonStatus();
    setInterval(updateDaemonStatus, 5000);
});

// ============================================================================
// Настройка Marked.js
// ============================================================================

function setupMarked() {
    // Конфигурация marked
    if (typeof marked !== 'undefined') {
        marked.setOptions({
            breaks: true,
            gfm: true
        });
    }
}

// ============================================================================
// Модальное окно "Добавить встречу"
// ============================================================================

function setupAddMeetingModal() {
    const modal = document.getElementById('addMeetingModal');
    const openBtns = document.querySelectorAll('#addMeetingBtn, #emptyAddBtn');
    const closeBtn = document.getElementById('modalCloseBtn');
    const cancelBtn = document.getElementById('modalCancelBtn');
    const submitBtn = document.getElementById('modalSubmitBtn');

    // Открытие модального окна
    openBtns.forEach(btn => {
        btn.addEventListener('click', openAddMeetingModal);
    });

    // Закрытие модального окна
    closeBtn.addEventListener('click', closeAddMeetingModal);
    cancelBtn.addEventListener('click', closeAddMeetingModal);

    // Закрытие при клике на фон
    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            closeAddMeetingModal();
        }
    });

    // Отправка формы
    submitBtn.addEventListener('click', submitAddMeeting);

    // Enter в последнем поле = отправка
    document.getElementById('meetingTitle').addEventListener('keypress', (e) => {
        if (e.key === 'Enter') {
            submitAddMeeting();
        }
    });

    document.getElementById('meetingUrl').addEventListener('keypress', (e) => {
        if (e.key === 'Enter') {
            submitAddMeeting();
        }
    });
}

function openAddMeetingModal() {
    const modal = document.getElementById('addMeetingModal');
    modal.classList.add('active');

    // Фокус на первый input
    document.getElementById('meetingUrl').focus();

    // Очищаем поля
    document.getElementById('meetingUrl').value = '';
    document.getElementById('meetingTitle').value = '';
}

function closeAddMeetingModal() {
    const modal = document.getElementById('addMeetingModal');
    modal.classList.remove('active');
}

async function submitAddMeeting() {
    const urlInput = document.getElementById('meetingUrl');
    const titleInput = document.getElementById('meetingTitle');
    const submitBtn = document.getElementById('modalSubmitBtn');

    const url = urlInput.value.trim();
    const title = titleInput.value.trim() || 'Встреча вручную';

    // Валидация
    if (!url) {
        showNotification('⚠️ Введите URL встречи', 'warning');
        urlInput.focus();
        return;
    }

    // Попытка парсить как URL
    try {
        new URL(url);
    } catch {
        showNotification('❌ Некорректный URL', 'error');
        return;
    }

    // Отключаем кнопку
    submitBtn.disabled = true;
    const originalText = submitBtn.innerHTML;
    submitBtn.innerHTML = '<span class="btn-emoji">⏳</span>Создаю...';

    try {
        const response = await fetch('/api/start-manual', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ url, title })
        });

        const data = await response.json();

        if (response.ok) {
            showNotification(`✅ Встреча "${title}" создана!`, 'success');
            closeAddMeetingModal();

            // Перенаправляем на страницу сессии
            setTimeout(() => {
                window.location.href = `/session/${data.session_id}`;
            }, 1000);
        } else {
            showNotification(`❌ ${data.error || 'Ошибка создания'}`, 'error');
        }
    } catch (error) {
        console.error('Ошибка сети:', error);
        showNotification('❌ Ошибка сети', 'error');
    } finally {
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalText;
    }
}

// ============================================================================
// Статус демона
// ============================================================================

async function updateDaemonStatus() {
    try {
        const response = await fetch('/api/daemon-status');
        const data = await response.json();

        const indicator = document.getElementById('daemonIndicator');
        const text = document.getElementById('daemonText');

        if (data.daemon_active) {
            indicator.classList.add('active');
            text.textContent = 'Демон активен';
        } else {
            indicator.classList.remove('active');
            text.textContent = 'Демон неактивен';
        }
    } catch (error) {
        console.error('Ошибка проверки демона:', error);
        const indicator = document.getElementById('daemonIndicator');
        indicator.classList.remove('active');
    }
}

// ============================================================================
// Уведомления
// ============================================================================

function showNotification(message, type = 'info') {
    const notification = document.getElementById('notification');
    if (!notification) return;

    notification.textContent = message;
    notification.className = `notification notification-${type} active`;

    setTimeout(() => {
        notification.classList.remove('active');
    }, 3000);
}

// ============================================================================
// Вспомогательные функции
// ============================================================================

/**
 * Экранирование HTML спецсимволов
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Форматирование времени
 */
function formatDate(date) {
    return new Intl.DateTimeFormat('ru-RU', {
        year: 'numeric',
        month: 'long',
        day: 'numeric'
    }).format(date);
}

/**
 * Форматирование дюрации (секунды -> "mm:ss")
 */
function formatDuration(seconds) {
    if (!seconds || seconds < 0) return '00:00';
    const minutes = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
}

/**
 * Копирование в буфер обмена
 */
async function copyToClipboard(text) {
    try {
        await navigator.clipboard.writeText(text);
        return true;
    } catch (error) {
        console.error('Ошибка копирования:', error);
        return false;
    }
}

/**
 * Получение инициалов из имени
 */
function getInitials(name) {
    return name
        .split(' ')
        .map(word => word[0])
        .join('')
        .toUpperCase()
        .slice(0, 2);
}

/**
 * Цвет аватара по индексу
 */
function getAvatarColor(index) {
    const colors = [
        '#6366f1', // indigo
        '#8b5cf6', // purple
        '#ec4899', // pink
        '#f59e0b', // amber
        '#10b981'  // green
    ];
    return colors[index % colors.length];
}

// ============================================================================
// Обработка ошибок
// ============================================================================

// Глобальный обработчик ошибок
window.addEventListener('error', (event) => {
    console.error('Глобальная ошибка:', event.error);
    showNotification('❌ Произошла неожиданная ошибка', 'error');
});

// Обработчик необработанных промисов
window.addEventListener('unhandledrejection', (event) => {
    console.error('Необработанный промис:', event.reason);
    showNotification('❌ Ошибка при выполнении операции', 'error');
});

// ============================================================================
// Полифиллы (если нужны)
// ============================================================================

// Полифилл для Object.entries если не поддерживается
if (!Object.entries) {
    Object.defineProperty(Object, 'entries', {
        value: function(obj) {
            return Object.keys(obj).map(key => [key, obj[key]]);
        }
    });
}
