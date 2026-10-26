'use strict';
/* Public bank quotes only; no dependency on the selected company or report API. */
window.CurrencyWidget = (() => {
  const source = 'https://alfabank.ru/currency/';
  const symbols = {CNY:'¥',EUR:'€',USD:'$'};
  let data = {status:'loading', rates:[]}, inFlight = false, timer;
  const node = id => document.getElementById(id);
  const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = value => new Intl.NumberFormat('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:4}).format(Number(value));
  function time(value) {
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? '' : new Intl.DateTimeFormat('ru-RU',
      {timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(d) + ' МСК';
  }
  function render() {
    if (!node('conv-body')) return;
    node('h-conv').textContent = 'Конвертация';
    node('conv-head').innerHTML = '<th>Валюта</th><th>Продать</th><th>Купить</th>';
    const rows = new Map((data.rates || []).map(r=>[r.code,r]));
    node('conv-body').innerHTML = ['CNY','EUR','USD'].map(code => {
      const r = rows.get(code);
      return '<tr><td><i class="cur">'+symbols[code]+'</i>'+code+'/RUB</td>'+
        '<td class="tnum"'+(r?' title="Курс на '+esc(time(r.rate_at))+'"':'')+'>'+(r?esc(number(r.sell_to_bank)):'—')+'</td>'+
        '<td class="tnum"'+(r?' title="Курс на '+esc(time(r.rate_at))+'"':'')+'>'+(r?esc(number(r.buy_from_bank)):'—')+'</td></tr>';
    }).join('');
    const status = data.status === 'stale' ? 'Не удалось обновить — последние полученные курсы. ' :
      !rows.size ? (data.status === 'loading' ? 'Загружаем курсы… ' : 'Курсы временно недоступны. ') : '';
    const dates = [...new Set((data.rates || []).map(r=>time(r.rate_at)))];
    const effective = dates.length === 1 ? 'Курсы на '+dates[0]+'. ' : '';
    node('conv-note').innerHTML = esc(status)+(rows.size?esc('Наличные · USD новых серий. '+effective):'')+
      '<a href="'+source+'" target="_blank" rel="noopener noreferrer">На сайте банка</a>';
    node('conv-note').title = data.fetched_at ? 'Проверено '+time(data.fetched_at)+'. Точный курс — на момент обмена в банке.' : '';
    node('w-conv').setAttribute('aria-busy',String(data.status === 'loading'));
  }
  async function refresh() {
    if (inFlight || document.hidden) return;
    clearTimeout(timer); inFlight = true;
    try {
      const response = await fetch('/api/currency-rates',{cache:'no-store',signal:AbortSignal.timeout(10000)});
      if (!response.ok) throw new Error('Currency request failed');
      const result = await response.json();
      if (!Array.isArray(result.rates)) throw new Error('Invalid currency response');
      data = result;
    } catch (_) {
      const age = Date.now() - new Date(data.fetched_at).getTime();
      const keep = Number.isFinite(age) && age >= 0 && age <= 86400000;
      data = {...data,rates:keep?data.rates:[],refreshing:false,status:keep&&data.rates.length?'stale':'unavailable'};
    } finally {
      inFlight = false; render();
      timer = setTimeout(refresh,data.refreshing ? 2000 : 60000);
    }
  }
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  render(); refresh();
  return {render,refresh};
})();
