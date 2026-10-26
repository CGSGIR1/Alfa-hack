/* Dashboard motion: no requests, models or report processing. */
'use strict';
window.SiteMotion=(()=>{
  const reduced=matchMedia('(prefers-reduced-motion:reduce)');
  const mouse=matchMedia('(hover:hover) and (pointer:fine)');
  let scrollFrame=null,targetY=0,positionY=0,previousTime=0,lastDirection=0;
  let revision=0,leaving=null,entering=null;
  const limit=()=>Math.max(0,document.documentElement.scrollHeight-innerHeight);
  const clamp=value=>Math.max(0,Math.min(limit(),value));
  function stopScroll(){
    if(scrollFrame!==null)cancelAnimationFrame(scrollFrame);
    scrollFrame=null;previousTime=0;lastDirection=0;targetY=positionY=scrollY;
  }
  function instant(y){window.scrollTo({top:y,left:scrollX,behavior:'instant'});}
  function frame(time){
    scrollFrame=null;
    const dt=previousTime?Math.min(40,time-previousTime):16;
    previousTime=time;targetY=clamp(targetY);positionY=clamp(positionY);
    // Accumulate fractional motion separately: rounded browser positions
    // must not stall the final easing frames, including high-refresh screens.
    const remaining=targetY-positionY;
    if(Math.abs(remaining)<.8){positionY=targetY;instant(targetY);previousTime=0;return;}
    positionY+=remaining*(1-Math.exp(-dt/62));instant(positionY);
    scrollFrame=requestAnimationFrame(frame);
  }
  function inNestedScroller(element){
    for(let node=element;node&&node!==document.body;node=node.parentElement){
      if(node.matches('input,textarea,select,[contenteditable="true"],[role="listbox"]'))return true;
      const style=getComputedStyle(node);
      if(/auto|scroll/.test(style.overflowY)&&node.scrollHeight>node.clientHeight+1)return true;
    }
    return false;
  }
  function wheel(event){
    if(reduced.matches||!mouse.matches||event.defaultPrevented||event.ctrlKey||event.metaKey||event.shiftKey||Math.abs(event.deltaX)>1)return;
    // Line/page wheels or discrete 80/100/120px ticks. High-resolution
    // touchpad gestures continue through the browser's native scrolling.
    const tick=event.deltaMode!==0||(Number.isInteger(event.deltaY)&&Math.abs(event.deltaY)>=80&&Math.abs(event.deltaY)%10===0);
    if(!tick||!event.deltaY||inNestedScroller(event.target))return;
    const delta=event.deltaY*(event.deltaMode===1?18:event.deltaMode===2?innerHeight:.9);
    const direction=Math.sign(delta);
    if(scrollFrame===null||direction!==lastDirection)targetY=positionY=scrollY;
    const next=clamp(targetY+delta);
    if(next===scrollY&&scrollFrame===null)return;
    event.preventDefault();targetY=next;lastDirection=direction;
    if(scrollFrame===null)scrollFrame=requestAnimationFrame(frame);
  }
  window.addEventListener('wheel',wheel,{passive:false});
  // Direct input and keyboard navigation always take over immediately.
  window.addEventListener('pointerdown',stopScroll,{passive:true});
  window.addEventListener('touchstart',stopScroll,{passive:true});
  window.addEventListener('keydown',event=>{
    if(['ArrowUp','ArrowDown','PageUp','PageDown','Home','End',' ','Escape'].includes(event.key))stopScroll();
  });
  window.addEventListener('resize',stopScroll,{passive:true});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)stopScroll();});
  reduced.addEventListener('change',stopScroll);
  function top(){stopScroll();instant(0);}
  function toElement(element){
    stopScroll();element.scrollIntoView({behavior:reduced.matches?'instant':'smooth',block:'start'});
  }
  function switchView(next,commit){
    const token=++revision;
    if(leaving)leaving.cancel();if(entering)entering.cancel();
    leaving=entering=null;stopScroll();
    const prev=document.querySelector('main.view:not([hidden])');
    const reveal=()=>{
      if(token!==revision)return;
      commit();
      if(!reduced.matches&&next!==prev&&next.animate){
        entering=next.animate([{opacity:0,transform:'translateY(10px)'},{opacity:1,transform:'translateY(0)'}],
          {duration:420,easing:'cubic-bezier(.16,1,.3,1)'});
      }
    };
    if(reduced.matches||!prev||prev===next||!prev.animate){reveal();return;}
    leaving=prev.animate([{opacity:1,transform:'translateY(0)'},{opacity:0,transform:'translateY(-5px)'}],
      {duration:135,easing:'cubic-bezier(.4,0,1,1)',fill:'forwards'});
    const exit=leaving;
    exit.finished.then(()=>{if(token!==revision)return;exit.cancel();leaving=null;reveal();},()=>{});
  }
  return{switchView,top,toElement};
})();

/* Card parallax. */
'use strict';
(()=>{
  const promo=document.querySelector('.home-card-promo');
  if(!promo)return;
  const enabled=matchMedia('(hover:hover) and (pointer:fine) and (prefers-reduced-motion:no-preference)');
  const properties=['--card-x','--card-y','--card-pitch','--card-yaw','--badge-x','--badge-y','--badge-turn','--copy-x','--copy-y'];
  let pending=null,position={x:0,y:0};
  const clamp=value=>Math.max(-1,Math.min(1,value));
  function reset(){
    if(pending!==null)cancelAnimationFrame(pending);
    pending=null;
    for(const property of properties)promo.style.removeProperty(property);
  }
  promo.addEventListener('pointermove',event=>{
    if(!enabled.matches||event.pointerType==='touch')return;
    const rect=promo.getBoundingClientRect();
    position={x:clamp((event.clientX-rect.left)/rect.width*2-1),y:clamp((event.clientY-rect.top)/rect.height*2-1)};
    if(pending!==null)return;
    pending=requestAnimationFrame(()=>{
      pending=null;
      const {x,y}=position;
      promo.style.setProperty('--card-x',(x*5).toFixed(2)+'px');
      promo.style.setProperty('--card-y',(y*3).toFixed(2)+'px');
      promo.style.setProperty('--card-pitch',(-y*3).toFixed(2)+'deg');
      promo.style.setProperty('--card-yaw',(x*4).toFixed(2)+'deg');
      promo.style.setProperty('--badge-x',(x*9).toFixed(2)+'px');
      promo.style.setProperty('--badge-y',(y*5).toFixed(2)+'px');
      promo.style.setProperty('--badge-turn',(x*3).toFixed(2)+'deg');
      promo.style.setProperty('--copy-x',(x*1.5).toFixed(2)+'px');
      promo.style.setProperty('--copy-y',y.toFixed(2)+'px');
    });
  });
  promo.addEventListener('pointerleave',reset);
  promo.addEventListener('pointercancel',reset);
  enabled.addEventListener('change',reset);
  document.addEventListener('visibilitychange',()=>{if(document.hidden)reset();});
  window.addEventListener('pagehide',reset);
})();


/* Theme preference is independent of company selection and report state. */
(()=>{
  const toggle=document.querySelector('.home-theme');if(!toggle)return;
  const key='alfa-ui-theme-v1';
  try {const saved=localStorage.getItem(key);if(saved==='light'||saved==='dark')document.documentElement.dataset.theme=saved;}catch{}
  function sync(){toggle.setAttribute('aria-pressed',String(document.documentElement.dataset.theme==='dark'));}
  toggle.addEventListener('click',()=>{
    const theme=document.documentElement.dataset.theme==='dark'?'light':'dark';
    document.documentElement.dataset.theme=theme;sync();
    try {localStorage.setItem(key,theme);}catch{}
  });sync();
})();

/* Live application. */

/* ---- app.js ---- */
(async function () {
  "use strict";
  var A = await window.SiteAdapter.init();
  window.NewsAdapter = A;
  var C = A.company;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var EXPO = "cubic-bezier(.16,1,.3,1)", P3 = "cubic-bezier(.215,.61,.355,1)";
  var NB = " ";

  /* ---------- Состояние (роль и фильтры сохраняются в браузере) ---------- */
  var KEY = "alfa-live-news-v2";
  var DEFAULT = { role: A.defaultRole, kind: "all", level: "all", topic: "all", period: A.getData().feed_period_default || "all" };
  var state = Object.assign({}, DEFAULT, load());
  state.role = A.defaultRole;
  function load() { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { return {}; } }
  /* Прочитанное хранится только в текущей вкладке: при новом открытии файла все новости снова непрочитанные */
  var RKEY = "alfa-live-read-v1:" + C.id;
  try { localStorage.removeItem("alfa-news-read-v1"); } catch (e) {}
  var readSet = (function () { try { return JSON.parse(sessionStorage.getItem(RKEY)) || {}; } catch (e) { return {}; } })();
  function saveRead() { try { sessionStorage.setItem(RKEY, JSON.stringify(readSet)); } catch (e) {} }
  function readVersion(id) {
    var item=A.getNews().find(function (n) { return n.id===id; });
    return item && item.notificationRevision || 1;
  }
  function isRead(id) { return readSet[id]===readVersion(id); }
  function save() { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} }

  /* ---------- Утилиты ---------- */
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function ico(id, cls) { return '<svg class="ico' + (cls ? " " + cls : "") + '" aria-hidden="true"><use href="#i-' + id + '"/></svg>'; }
  function plural(n, a, b, c) { var m = n % 10, h = n % 100; return m === 1 && h !== 11 ? a : m >= 2 && m <= 4 && (h < 10 || h >= 20) ? b : c; }
  var LEVEL = { high: "Высокое", medium: "Среднее", low: "Низкое" };
  function pill(kind) {
    if (kind === "risk") return '<span class="pill pill-risk">Риск</span>';
    if (kind === "opportunity") return '<span class="pill pill-opp">Возможность</span>';
    return '<span class="pill pill-none">Не размечено</span>';
  }
  function lvlText(n) {
    if(n.impact)return '<span class="lvl" data-level="'+n.level+'">Влияние: <b>'+LEVEL[n.level].toLowerCase()+'</b>'+(n.impact.conditional ? ' · при выполнении условий' : '')+'</span>';
    if (!n.drivers.length) return '<span class="lvl">Влияние не оценено</span>';
    return '<span class="lvl" data-level="' + n.level + '">Влияние: <b>' + LEVEL[n.level].toLowerCase() + "</b></span>";
  }
  function meterMini(n) {
    if(n.impact)return lvlText(n);
    if (!n.drivers.length) return '<span class="meter-mini">Влияние не оценено</span>';
    return '<span class="meter-mini meter" data-level="' + n.level + '" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + n.score + '" aria-label="Влияние на компанию: ' + LEVEL[n.level].toLowerCase() + '">' +
      "<span>" + LEVEL[n.level] + ' влияние</span><span class="meter-track"><span class="meter-fill" style="width:' + Math.max(n.score, 2) + '%"></span></span></span>';
  }
  function meterFull(n) {
    if(n.impact)return lvlText(n);
    return '<span class="meter" data-level="' + n.level + '" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + n.score + '">' +
      '<span class="meter-top"><b>' + LEVEL[n.level] + ' влияние</b><span class="tnum">' + n.score + NB + "из" + NB + "100</span></span>" +
      '<span class="meter-track"><span class="meter-fill" style="width:' + Math.max(n.score, 2) + '%"></span></span></span>';
  }
  function empty(text) { return '<p class="small muted">' + esc(text) + "</p>"; }
  var toastT;
  function toast(t) { var el = $("#toast"); el.textContent = t; el.hidden = false; clearTimeout(toastT); toastT = setTimeout(function () { el.hidden = true; }, 2400); }

  /* ---------- Шапка, роль ---------- */
  function roleLabel() { var r = A.roles.filter(function (x) { return x.key === state.role; })[0] || A.roles[0]; return r ? r.label : "Выберите компанию"; }
  var MONTHS = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"];
  $("[data-today]").textContent = "на " + A.today.getDate() + NB + MONTHS[A.today.getMonth()] + " " + A.today.getFullYear();
  var rs = $("#role-select");
  rs.innerHTML = A.roles.map(function (r) { return '<option value="' + r.key + '">' + esc(r.label) + "</option>"; }).join("");
  if (!A.roles.some(function (r) { return r.key === state.role; })) state.role = A.defaultRole;
  rs.value = state.role;
  rs.addEventListener("change", async function () {
    try {
      if (!await A.selectCompany(rs.value)) return;
      selectedCompany();
    } catch (error) { toast(error.message); rs.value = C.id; syncDD(); }
  });
  function selectedCompany() {
    C = A.company; state.role = C.id; state.topic = "all"; save();
    RKEY = "alfa-live-read-v1:" + C.id;
    try { readSet = JSON.parse(sessionStorage.getItem(RKEY)) || {}; } catch (_) { readSet = {}; }
    updateLiveView(); toast("Выбрана компания " + C.id);
  }

  var companyDialog=$("#add-company-dialog"),companyForm=$("#add-company-form");
  var companyInput=$("#add-company-id"),companyStatus=$("#add-company-status"),companyError=$("#add-company-error");
  var addingCompany=false;
  function openCompanyDialog() {
    if (!addingCompany) { companyError.hidden=true; companyStatus.textContent=""; }
    companyDialog.showModal();
    if (!addingCompany) companyInput.focus();
  }
  $("[data-close-company]").addEventListener("click",function(){companyDialog.close();});
  async function submitCompany(resumeJob) {
    if (addingCompany) return;
    var value=companyInput.value.trim();
    if (!resumeJob && !/^(?:U_)?[0-9]{1,20}$/i.test(value)) {
      companyError.textContent="Введите ID из CSV, например U_9250 или 9250.";
      companyError.hidden=false;companyInput.focus();return;
    }
    addingCompany=true;companyError.hidden=true;companyInput.disabled=true;
    var submit=companyForm.querySelector('[type="submit"]');submit.disabled=true;submit.textContent="Добавляем…";
    companyStatus.textContent="Подготавливаем компанию…";
    try {
      var id=await A.addCompany(value,function(phase){companyStatus.textContent=phase;},resumeJob);
      if(await A.selectCompany(id)) selectedCompany();
      companyDialog.close();companyInput.value="";
    } catch(error) {
      companyError.textContent=error.message;companyError.hidden=false;
      companyStatus.textContent="Можно повторить попытку. Готовые данные сохраняются.";
    } finally {
      addingCompany=false;companyInput.disabled=false;submit.disabled=false;submit.textContent="Добавить компанию";
    }
  }
  companyForm.addEventListener("submit",function(e){e.preventDefault();submitCompany(null);});

  /* ---------- Главная ---------- */
  function short(p) { return String(p || "").split(",")[0]; }
  /* Вариант Б: карусель новостей рядом со счётом */
  var car = { list: [], i: 0 };
  function showSlide(dir, keep) {
    var n = car.list[car.i]; if (!n) {
      $("#bhn-slide").href = "#analytics"; $("#bhn-slide").removeAttribute("data-open");
      $("#bhn-m").textContent = "Новостная аналитика";
      $("#bhn-t").textContent = "Для компании пока нет новых готовых материалов";
      $("#bhn-count").textContent = "0 из 0";
      $("#bhn-prev").disabled = true; $("#bhn-next").disabled = true; return;
    }
    if (keep) dir = 0;
    var slide = $("#bhn-slide");
    slide.href = "#news-" + n.id; slide.setAttribute("data-open", n.id);
    $("#bhn-m").innerHTML = (isRead(n.id) ? "" : '<span class="udot" role="img" aria-label="Не прочитано"></span>') + esc(n.topic + " · " + n.dateLabel);
    $("#bhn-t").textContent = n.title;
    $("#bhn-count").textContent = (car.i + 1) + " из " + car.list.length;
    $("#bhn-prev").disabled = car.i === 0; $("#bhn-next").disabled = car.i === car.list.length - 1;
    if (dir && !reduce && slide.animate) slide.animate([{ opacity: 0, transform: "translateX(" + (dir * 24) + "px)" }, { opacity: 1, transform: "none" }], { duration: 360, easing: EXPO });
  }
  function step(d) { var j = Math.max(0, Math.min(car.list.length - 1, car.i + d)); if (j !== car.i) { car.i = j; showSlide(d); } }
  $("#bhn-prev").addEventListener("click", function (e) { e.stopPropagation(); step(-1); });
  $("#bhn-next").addEventListener("click", function (e) { e.stopPropagation(); step(1); });
  $("#bh-news").addEventListener("click", function (e) {
    if (e.target.closest(".bhn-btn") || e.target.closest("a")) return;
    $("#bhn-slide").click();
  });
  $("#bh-news").addEventListener("keydown", function (e) {
    if (e.key === "ArrowRight") { e.preventDefault(); step(1); } else if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
  });
  function renderImportant(news) {
    car.list = news.filter(function (n) { return n.kind; }).slice(0, 5); car.i = 0; showSlide(0);
    var top = news.filter(function (n) { return n.kind; }).slice(0, 3);
    $("#cab-news-list").innerHTML = top.map(function (n) {
      return '<li><a href="#news-' + n.id + '" data-open="' + n.id + '"><span class="m">' + esc(n.topic) + " · " + esc(n.dateLabel) + '</span><span class="t">' + esc(n.title) + "</span></a></li>";
    }).join("");
  }

  /* ---------- Раскрытие ---------- */
  var running = new WeakMap();
  function expand(el, open) {
    var prev = running.get(el); if (prev) prev.cancel();
    if (reduce || !el.animate) { el.hidden = !open; return; }
    el.hidden = false;
    var cs = getComputedStyle(el);
    var full = { height: el.scrollHeight + "px", paddingTop: cs.paddingTop, paddingBottom: cs.paddingBottom, opacity: 1 };
    var zero = { height: "0px", paddingTop: "0px", paddingBottom: "0px", opacity: 0 };
    el.style.overflow = "hidden";
    var anim = el.animate(open ? [zero, full] : [full, zero], { duration: open ? 340 : 260, easing: open ? P3 : "cubic-bezier(.4,0,.2,1)" });
    running.set(el, anim);
    anim.onfinish = function () { el.style.overflow = ""; if (!open) el.hidden = true; running.delete(el); };
    anim.oncancel = function () { el.style.overflow = ""; };
  }

  /* ---------- Свои выпадающие списки вместо системных ---------- */
  var DDS = [];
  function enhanceSelect(sel, variant) {
    var wrap = sel.parentNode; wrap.classList.add("dd");
    var btn = document.createElement("button");
    btn.type = "button"; btn.id = sel.id + "-btn";
    btn.className = "dd-btn " + (variant === "role" ? "dd-role" : "select");
    btn.setAttribute("aria-haspopup", "listbox"); btn.setAttribute("aria-expanded", "false");
    var menu=document.createElement("div");menu.className="dd-menu";menu.hidden=true;
    var list = document.createElement("ul");
    list.className = "dd-list"; list.id = sel.id + "-list"; list.setAttribute("role", "listbox"); list.hidden = true;
    btn.setAttribute("aria-controls", list.id);
    sel.classList.add("dd-native"); sel.tabIndex = -1; sel.setAttribute("aria-hidden", "true");
    wrap.insertBefore(btn, sel); menu.appendChild(list);wrap.appendChild(menu);
    var addButton=null;
    if (variant === "role") {
      addButton=document.createElement("button");addButton.type="button";addButton.className="dd-add-company";
      addButton.textContent="+ Добавить компанию";menu.appendChild(addButton);
      addButton.addEventListener("click",function(e){e.stopPropagation();close();openCompanyDialog();});
      addButton.addEventListener("keydown",function(e){
        if(e.key === "ArrowUp") {e.preventDefault();if(list.lastElementChild)list.lastElementChild.focus();}
        if(e.key === "Escape") {e.preventDefault();close(true);}
      });
    }
    function sync() {
      var o = sel.options[sel.selectedIndex]; btn.textContent = o ? o.text : "";
    }
    function build() {
      list.innerHTML = Array.prototype.map.call(sel.options, function (o) {
        var on = o.value === sel.value;
        return '<li role="option" tabindex="-1" data-value="' + esc(o.value) + '" aria-selected="' + on + '"><span>' + esc(o.text) + "</span>" + ico("check") + "</li>";
      }).join("");
    }
    function open() {
      DDS.forEach(function (d) { if (d.close !== close) d.close(); });
      build(); menu.hidden=false;list.hidden = false; wrap.classList.add("open"); btn.setAttribute("aria-expanded", "true");
      if (!reduce && list.animate) list.animate([{ opacity: 0, transform: "translateY(-6px) scale(.98)" }, { opacity: 1, transform: "none" }], { duration: 200, easing: EXPO });
      var cur = list.querySelector('[aria-selected="true"]') || list.firstElementChild || addButton; if (cur) cur.focus();
    }
    function close(focusBtn) {
      if (list.hidden) return;
      menu.hidden=true;list.hidden = true; wrap.classList.remove("open"); btn.setAttribute("aria-expanded", "false");
      if (focusBtn) btn.focus();
    }
    function choose(v) {
      if (sel.value !== v) { sel.value = v; sel.dispatchEvent(new Event("change", { bubbles: true })); }
      sync(); close(true);
    }
    btn.addEventListener("click", function (e) { e.stopPropagation(); if (list.hidden) open(); else close(); });
    btn.addEventListener("keydown", function (e) { if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); open(); } });
    list.addEventListener("click", function (e) { e.stopPropagation(); var li = e.target.closest("li"); if (li) choose(li.getAttribute("data-value")); });
    list.addEventListener("keydown", function (e) {
      var items = Array.prototype.slice.call(list.children), i = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown") { e.preventDefault(); (items[i + 1] || addButton || items[0]).focus(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); (items[i - 1] || items[items.length - 1]).focus(); }
      else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); if (items[i]) choose(items[i].getAttribute("data-value")); }
      else if (e.key === "Escape") { e.preventDefault(); close(true); }
      else if (e.key === "Tab" && !addButton) { close(); }
    });
    menu.addEventListener("focusout",function(e){if(!menu.contains(e.relatedTarget))close();});
    sync();
    var d = { sync: sync, close: close }; DDS.push(d); return d;
  }
  document.addEventListener("click", function () { DDS.forEach(function (d) { d.close(); }); });
  function syncDD() { DDS.forEach(function (d) { d.sync(); }); }

  /* ---------- Аналитика ---------- */
  function renderWhy() {
    var on = C.products.filter(function (p) { return ["Кредит", "Лизинг", "Зарплатный проект"].indexOf(p.label) >= 0 && p.on; }).map(function (p) { return p.label.toLowerCase(); });
  }
  function inPeriod(n) { return state.period === "all" || (n.ageDays >= 0 && n.ageDays < Number(state.period)); }
  function filtered(news) {
    return news.filter(function (n) {
      if (!inPeriod(n)) return false;
      if (state.kind === "risk" && !n.hasRisks) return false;
      if (state.kind === "opportunity" && !n.hasOpportunities) return false;
      if (state.topic !== "all" && n.topic !== state.topic) return false;
      return true;
    });
  }
  function impactHtml(n) {
    if(n.impact)return '<div class="imp-head">'+lvlText(n)+'</div>';
    if (!n.drivers.length) return '<div class="imp-head">' + empty("Влияние на компанию не оценено: событие не размечено") + "</div>";
    var ar = A.areas.map(function (a) {
      var v = n.areas[a.key];
      if (v === null || Math.abs(v) < 0.01) return "";
      return '<span><b>' + a.label + "</b> — " + (v < 0 ? '<span class="r">риск</span>' : '<span class="o">возможность</span>') + "</span>";
    }).filter(Boolean).join("");
    var drv = '<ul class="drivers">' + n.drivers.map(function (d) {
      return '<li class="drv"><span class="lab">' + (d.sign < 0 ? '<span class="sw sw-risk"></span>' : '<span class="sw sw-opp"></span>') + esc(d.label) + (d.roleFocus ? ' <span class="focus-tag">важно для роли</span>' : "") + "</span>" +
        '<span class="ev">' + esc(d.evidence.join("; ")) + "</span>" +
        '<span class="sh tnum">' + Math.round(d.share * 100) + "%</span></li>";
    }).join("") + "</ul>";
    return '<div class="imp-head"><span>Влияние на компанию: <b>' + LEVEL[n.level].toLowerCase() + '</b> <span class="muted tnum">(' + n.score + NB + "из" + NB + '100)</span></span><span class="ar">' + ar + "</span></div>";
  }
  function kindNote(n) {
    if (!n.kind || !LEVEL[n.level]) return "";
    var k = n.kind === "risk" ? '<b class="r">риск</b>' : '<b class="o">возможность</b>';
    return '<p class="kind-note">Для компании это ' + k + ", влияние " + LEVEL[n.level].toLowerCase() + ".</p>";
  }
  function list(items) { return '<ul class="dots">' + items.map(function (f) { return "<li>" + esc(f) + "</li>"; }).join("") + "</ul>"; }
  var CARD_TABS = true;
  function actWord(k) { return plural(k, "рекомендация", "рекомендации", "рекомендаций"); }
  function tabsBody(n) { return window.SitePanels.render(n); }
  function showTab(card, key, focus) {
    $$(".nbt-tab", card).forEach(function (t) { var on = t.getAttribute("data-tab") === key; t.setAttribute("aria-selected", on); t.tabIndex = on ? 0 : -1; if (on && focus) t.focus(); });
    $$(".nbt-panel", card).forEach(function (p) {
      var on = p.id === "p-" + card.getAttribute("data-id") + "-" + key;
      if (on && p.hidden) { p.hidden = false; if (!reduce && p.animate) p.animate([{ opacity: 0, transform: "translateY(6px)" }, { opacity: 1, transform: "none" }], { duration: 260, easing: EXPO }); }
      else if (!on) p.hidden = true;
    });
  }
  document.addEventListener("click", function (e) {
    var t = e.target.closest(".nbt-tab, .nbt-go"); if (!t) return;
    showTab(t.closest(".card"), t.getAttribute("data-tab"), t.classList.contains("nbt-go"));
  });
  document.addEventListener("keydown", function (e) {
    var t = e.target.closest && e.target.closest(".nbt-tab"); if (!t || (e.key !== "ArrowRight" && e.key !== "ArrowLeft")) return;
    e.preventDefault();
    var tabs = $$(".nbt-tab", t.parentNode), i = tabs.indexOf(t), n = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    showTab(t.closest(".card"), n.getAttribute("data-tab"), true);
  });
  function cardHtml(n) {
    var m = n.model;
    return '<article class="card' + (isRead(n.id) ? "" : " unread") + '" id="news-' + n.id + '" data-id="' + n.id + '">' +
      '<button class="card-row" type="button" aria-expanded="false" aria-controls="body-' + n.id + '">' +
        "<span>" +
          '<span class="meta"><span class="udot" role="img" aria-label="Не прочитано"></span><span>' + esc(n.record.independent ? "Контекст компании · " + n.topic : n.topic) + "</span><span>" + esc(n.dateLabel) + "</span></span>" +
          "<h3>" + esc(n.title) + "</h3>" +
        "</span>" +
        '<span class="card-side"><span class="round" aria-hidden="true">' + ico("caret-down") + "</span></span>" +
      "</button>" +
      '<div class="card-body" id="body-' + n.id + '" hidden>' + (CARD_TABS ? tabsBody(n) + '<div class="nb" hidden>' : '<div class="nb">') +
        (m && m.meaning ? '<p class="nb-lead">' + esc(m.meaning) + "</p>" : "") +
        (n.figures.length ? '<div class="nb-figs">' + n.figures.map(function (f) {
          return '<div class="nb-fig' + (f.accent ? " acc" : "") + '"><b class="tnum">' + esc(f.value) + "</b><span>" + esc(f.label) + "</span></div>";
        }).join("") + "</div>" : "") +
        '<div class="nb-sec"><span class="nb-lab">Почему это важно для вас</span>' +
          (m && m.rationale ? '<p class="nb-text">' + esc(m.rationale) + "</p>" : empty("Пояснение не подготовлено")) + "</div>" +
        '<div class="nb-sec"><span class="nb-lab">Что сделать</span>' +
          (m && m.actions.length ? '<div class="nb-acts">' + m.actions.map(function (x) { return '<div class="nb-act">' + esc(x) + "</div>"; }).join("") + "</div>" : empty("Реакция не требуется")) + "</div>" +
        '<details class="nb-more"><summary>Подробнее: что произошло и мнение банка' + ico("caret-down") + '</summary><div class="nb-more-in">' +
          '<div><span class="nb-lab">Что произошло</span>' + (n.facts.length ? list(n.facts) : empty("Факты не извлечены")) + "</div>" +
          '<div><span class="nb-lab">Мнение Альфа-Банка</span>' + (n.bankView ? '<p class="nb-text">' + esc(n.bankView) + "</p>" : empty("Позиция банка в материале не найдена")) + "</div>" +
        "</div></details>" +
      "</div></div></article>";
  }
  function fmtNum(v, unit) { var s = String(v).replace(".", ","); return unit && unit.indexOf("%") === 0 ? s + "%" : s + (unit === "руб." ? NB + "₽" : ""); }
  function fmtPct(p) { if (p === null || p === undefined) return ""; if (p === 0) return "без изменений"; return (p > 0 ? "+" : p < 0 ? "−" : "") + String(Math.abs(p)).replace(".", ",") + "%"; }
  function marketHtml(list) {
    return '<div class="nb-figs nbt-figs">' + list.map(function (m) {
      var nm = m.code === "key_rate" ? "ключевая ставка" : m.name.replace("Официальный курс ", "").replace("/RUB", "");
      return '<div class="nb-fig"><b class="tnum">' + esc(fmtNum(m.value, m.unit)) + "</b><span>" + esc(nm) + '</span><span class="nb-delta tnum">' + esc(m.change_1w && m.change_1w.pct === 0 ? "без изменений за неделю" : fmtPct(m.change_1w && m.change_1w.pct) + " за неделю") + "</span></div>";
    }).join("") + '</div><p class="nbt-src">' + esc((list[0] && list[0].source) || "") + ", на " + esc(dlab(list[0] && list[0].date)) + "</p>";
  }
  function dlab(iso) { if (!iso) return ""; var d = new Date(iso + "T12:00:00+03:00"); return d.getDate() + NB + MONTHS[d.getMonth()]; }

  function renderReportBlocks() {
    if (A.mode !== "report") return;
    var sum = $("#an-sum");
    sum.hidden=!A.summary;sum.textContent=A.summary || '';
    if (A.summary) {
      sum.textContent = (A.getData().latest_report?.as_of ? "Отчёт на " + dlab(A.getData().latest_report.as_of) + " " + A.getData().latest_report.as_of.slice(0,4) + ". " : "") + A.summary;
      var kr = $("#an-recs");
      if (false && A.keyRecs && A.keyRecs.length) { kr.innerHTML = '<span class="an-recs-h">Ключевые рекомендации</span><ol>' + A.keyRecs.map(function (t) { return "<li>" + esc(t) + "</li>"; }).join("") + "</ol>"; kr.hidden = false; }
      sum.hidden = false;
    }
  }
  function renderMarketWidget() {
    if (window.CurrencyWidget) window.CurrencyWidget.render();
  }
  function renderStats(news) {
    var p = news.filter(inPeriod);
    var r = p.filter(function (n) { return n.kind === "risk"; }).length;
    var o = p.filter(function (n) { return n.kind === "opportunity"; }).length;
    var h = p.filter(function (n) { return n.level === "high"; }).length;
    var tile = function (v, label, icon) { return '<div class="stat"><div><b class="tnum">' + v + "</b><span>" + label + '</span></div><span class="tile-ico">' + ico(icon) + "</span></div>"; };
    $("#stats").innerHTML = tile(r, plural(r, "риск", "риска", "рисков"), "warning-circle") + tile(o, plural(o, "возможность", "возможности", "возможностей"), "trend-up") + tile(h, "с высоким влиянием", "lightning");
  }
  function isFiltered() { return state.kind !== "all" || state.topic !== "all" || state.period !== DEFAULT.period; }
  function bindCards(container) {
    $$(container + " .card-row").forEach(function (b) {
      b.addEventListener("click", function () {
        var body = b.nextElementSibling, open = b.getAttribute("aria-expanded") === "true";
        b.setAttribute("aria-expanded", String(!open)); expand(body, !open);
        if (!open) markRead(b.closest(".card").getAttribute("data-id"));
      });
    });
  }
  function renderFeed(news) {
    var list = filtered(news);
    var cnt = function (k) { return news.filter(function (n) { return inPeriod(n) && (k === "all" || n.kind === k); }).length; };
    $("#f-kind").value = state.kind; $("#f-topic").value = state.topic; $("#f-period").value = state.period;
    $("#f-reset").hidden = !isFiltered();
    syncDD();
    var emptyByFilters = isFiltered() && news.length > 0;
    $("#feed").innerHTML = list.length ? list.map(cardHtml).join("") :
      '<div class="plate feed-empty" role="status">' +
        '<img class="feed-empty-art" src="/assets/empty-news-v1.webp" width="768" height="512" alt="" draggable="false" decoding="async">' +
        '<div class="feed-empty-copy"><h3>' + (emptyByFilters ? 'По выбранным фильтрам новостей нет' : 'Пока нет подходящих новостей') + '</h3>' +
        '<p class="muted">' + (emptyByFilters ? 'Попробуйте изменить фильтры, чтобы увидеть другие материалы.' : 'Мы следим за новостями. Подходящие для вашей компании материалы появятся здесь автоматически.') + '</p>' +
        (isFiltered() ? '<button class="btn btn-grey btn-s feed-empty-reset" type="button" data-reset>Сбросить фильтры</button>' : '') + '</div></div>';
    bindCards("#feed");
    var r = $("#feed [data-reset]"); if (r) r.addEventListener("click", resetFilters);
  }
  function markRead(id) {
    if (!id || isRead(id)) return;
    readSet[id] = readVersion(id); saveRead();
    var card = document.getElementById("news-" + id);
    if (card) {
      var dot = $(".udot", card);
      if (dot && !reduce && dot.animate) dot.animate([{ transform: "scale(1)", opacity: 1 }, { transform: "scale(0)", opacity: 0 }], { duration: 260, easing: EXPO }).onfinish = function () { card.classList.remove("unread"); };
      else card.classList.remove("unread");
    }
    renderUnread();
  }
  function renderUnread() {
    var u = NEWS.filter(function (n) { return !isRead(n.id); }).length;
    $$("[data-unread]").forEach(function (el) { el.textContent = ""; el.setAttribute("data-count", u); el.hidden = u === 0; el.setAttribute("role", "img"); el.setAttribute("aria-label", "Есть непрочитанные новости"); });
    $("#unread-line").textContent = u ? u + " " + plural(u, "непрочитанная новость", "непрочитанные новости", "непрочитанных новостей") : "Все новости прочитаны";
    $("#mark-all").disabled = u === 0;
    showSlide(0, true);
  }
  $("#mark-all").addEventListener("click", function () {
    NEWS.forEach(function (n) { readSet[n.id] = readVersion(n.id); }); saveRead();
    $$("#feed .card.unread").forEach(function (c) { c.classList.remove("unread"); });
    renderUnread(); toast("Все новости отмечены прочитанными");
  });
  $("#reset-read").addEventListener("click", function () {
    readSet = {}; saveRead(); renderFeed(NEWS); renderUnread(); toast("Все новости снова непрочитанные");
  });
  function resetFilters() { state.kind = "all"; state.level = "all"; state.topic = "all"; state.period = DEFAULT.period; save(); renderAnalytics(); }

  $("#f-topic").innerHTML = '<option value="all">Все темы</option>' + A.topics().map(function (t) { return '<option value="' + esc(t) + '">' + esc(t) + "</option>"; }).join("");
  $("#f-kind").addEventListener("change", function (e) { state.kind = e.target.value; save(); renderAnalytics(); });
  $("#f-topic").addEventListener("change", function (e) { state.topic = e.target.value; save(); renderAnalytics(); });
  $("#f-period").addEventListener("change", function (e) { state.period = e.target.value; save(); renderAnalytics(); });
  $("#f-reset").addEventListener("click", resetFilters);

  var NEWS = [];
  function renderAnalytics() { renderFeed(NEWS); }
  function renderAll(animate) {
    NEWS = A.getNews(state.role);
    var high = NEWS.filter(function (n) { return n.level === "high"; }).length;
    $$("[data-badge]").forEach(function (b) { b.textContent = high; b.setAttribute("aria-label", high + " с высоким влиянием"); });
    renderImportant(NEWS);
    renderAnalytics();
    renderUnread();
  }

  /* ---------- Переходы между разделами ---------- */
  var current = null;
  function show(view, newsId) {
    var next = $("#view-" + view), changed = view !== current;
    function reveal() {
      document.title = view === "analytics" ? "Новостная аналитика · Альфа-Бизнес" : "Личный кабинет · Альфа-Бизнес";
      $$("[data-nav]").forEach(function(a) { if(a.getAttribute("data-nav") === view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
      $$("main.view").forEach(function(v) { v.hidden = v !== next; });
      if(changed) { if(window.SiteMotion) window.SiteMotion.top(); else window.scrollTo({top:0,behavior:"instant"}); }
      current = view;
      if(newsId) openNews(newsId);
    }
    if(current !== null && window.SiteMotion) window.SiteMotion.switchView(next,reveal); else reveal();
  }
  function openNews(id) {
    var card = document.getElementById("news-" + id);
    if (!card) { resetFilters(); card = document.getElementById("news-" + id); if (!card) return; }
    var b = $(".card-row", card), body = b.nextElementSibling;
    if (b.getAttribute("aria-expanded") !== "true") { b.setAttribute("aria-expanded", "true"); body.hidden = false; }
    markRead(id);
    requestAnimationFrame(function () {
      if(window.SiteMotion) window.SiteMotion.toElement(card); else card.scrollIntoView({behavior:reduce?"instant":"smooth",block:"start"});
            b.focus({ preventScroll: true });
    });
  }
  function route() {
    var h = (location.hash || "").replace("#", "");
    if (h.indexOf("news-") === 0) show("analytics", h.slice(5));
    else show(h === "analytics" ? "analytics" : "home");
  }
  window.addEventListener("hashchange", route);
  document.addEventListener("click", function (e) {
    var s = e.target.closest("[data-soon]");
    if (s) {
      e.preventDefault();
      toast("Этот раздел пока недоступен");
      return;
    }
    var o = e.target.closest("[data-open]");
    if (o && location.hash === "#news-" + o.getAttribute("data-open")) { e.preventDefault(); route(); }
  });

  function updateLiveView() {
    var focused = document.activeElement, focusedCard = focused && focused.closest('.card');
    var focusId = focusedCard && focusedCard.id, focusTab = focused && focused.getAttribute('data-tab');
    var anchor = $$(".card").find(function (card) { return card.getBoundingClientRect().bottom > 0; });
    var anchorId = anchor && anchor.id, anchorTop = anchor && anchor.getBoundingClientRect().top;
    C = A.company;
    var openCards = $$(".card-row[aria-expanded='true']").map(function (b) {
      var card = b.closest(".card"), tab = $(".nbt-tab[aria-selected='true']", card);
      return {id:card.getAttribute("data-id"),tab:tab && tab.getAttribute("data-tab")};
    });
    var currentCompany = rs.value;
    rs.innerHTML = A.roles.map(function (r) { return '<option value="' + esc(r.key) + '">' + esc(r.label) + '</option>'; }).join("");
    rs.value = C.id;
    $("#f-topic").innerHTML = '<option value="all">Все темы</option>' + A.topics().map(function (t) { return '<option value="' + esc(t) + '">' + esc(t) + '</option>'; }).join("");
    if (!A.topics().includes(state.topic)) state.topic = "all";
    $("[data-today]").textContent = "на " + A.today.getDate() + NB + MONTHS[A.today.getMonth()] + " " + A.today.getFullYear();
    renderAll(false); renderReportBlocks(); renderMarketWidget(); syncDD();
    if (currentCompany === C.id) openCards.forEach(function (x) {
      var card = document.getElementById("news-"+x.id); if (!card) return;
      $(".card-row",card).setAttribute("aria-expanded","true"); $(".card-body",card).hidden = false;
      if (x.tab) showTab(card,x.tab,false);
    });
    if (currentCompany === C.id && anchorId && window.scrollY > 100) {
      var retained = document.getElementById(anchorId);
      if (retained) window.scrollBy({top:retained.getBoundingClientRect().top-anchorTop,left:0,behavior:"instant"});
    }
    if (currentCompany === C.id && focusId) {
      var focusCard = document.getElementById(focusId);
      var focusTarget = focusCard && (focusTab ? focusCard.querySelector('[data-tab="'+focusTab+'"]') : focusCard.querySelector('.card-row'));
      if (focusTarget) focusTarget.focus({preventScroll:true});
    }
  }
  A.onChange(updateLiveView);
  if (!Array.from($("#f-period").options).some(function (option) { return option.value === state.period; })) state.period = DEFAULT.period;
  $("label[for='role-select-btn']").textContent = "Выбор компании";
  $(".page-foot span").textContent = "Факты — из новостных источников. Оценки аналитиков и выводы ИИ показаны отдельно; материалы Альфа-Банка имеют специальную пометку.";
  renderWhy();
  enhanceSelect($("#role-select"), "role");
  enhanceSelect($("#f-topic")); enhanceSelect($("#f-kind")); enhanceSelect($("#f-period"));
  renderAll(true); renderReportBlocks(); renderMarketWidget();
  route();
  if (A.pendingCompany && A.pendingCompany()) {openCompanyDialog();submitCompany(A.pendingCompany());}
})().catch(function (error) {
  var message = document.createElement("p"); message.className = "muted"; message.textContent = "Не удалось загрузить новости. Попробуйте позже.";
  var feed = document.getElementById("feed"); if (feed) feed.replaceChildren(message);
  var headline = document.getElementById("bhn-t"); if (headline) headline.textContent = "Не удалось загрузить новости. Попробуйте позже.";
});

