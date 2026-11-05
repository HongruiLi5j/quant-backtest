/* ==========================================================
 * 文件: shared/theme-boot.js
 * 版本: 0.1.0 · 更新: 2026-10-23 · 作者: Claude（为李泓锐）
 * 用途: 放在 <head> 里同步执行的极小脚本（非 module），在首帧渲染前恢复主题、
 *       涨跌色，并给 <html> 加 .js 类，避免「先闪深色再变浅色」(FOUC)。
 *       不能写行内脚本，因为页面 CSP 只允许同源脚本。
 * ========================================================== */
(function () {
  const root = document.documentElement;
  root.classList.add('js');
  try {
    const theme = localStorage.getItem('qb-theme');
    // D95：没选过（或存的值不认识）就默认终端主题；用户手动选过的仍然优先
    root.setAttribute('data-theme', theme === 'light' || theme === 'dark' || theme === 'terminal' ? theme : 'terminal');
    const market = localStorage.getItem('qb-market');
    if (market === 'cn' || market === 'us') root.setAttribute('data-market', market);
  } catch (e) {
    /* 隐私模式下 localStorage 不可用，按默认值（终端）渲染 */
    root.setAttribute('data-theme', 'terminal');
  }
})();
