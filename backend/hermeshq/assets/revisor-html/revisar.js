'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const PLAZO_TOTAL_MS = Number(process.env.REVISOR_PLAZO_MS || 15000);
const PLAZO_CLICS_MS = Number(process.env.REVISOR_PLAZO_CLICS_MS || 6000);
const MAX_CLICS = 300;
const MAX_ERRORES = 5;
const TIPOS_JS = new Set(['', 'text/javascript', 'application/javascript', 'text/ecmascript', 'application/ecmascript']);
const IGNORADOS = /Not implemented|Could not load (img|link|script|iframe)|navigation/i;

const ruta = process.argv[2];
let terminado = false;

function salir(resultado) {
  if (terminado) return;
  terminado = true;
  process.stdout.write(JSON.stringify(resultado));
  process.exit(0);
}

function recortar(texto, largo) {
  const limpio = String(texto || '').replace(/\s+/g, ' ').trim();
  return limpio.length > largo ? limpio.slice(0, largo - 1) + '…' : limpio;
}

function scriptsEnLinea(html) {
  const scripts = [];
  const patron = /<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi;
  let m;
  while ((m = patron.exec(html)) !== null) {
    const atributos = m[1] || '';
    const tipo = (/\btype\s*=\s*["']?([^"'\s>]+)/i.exec(atributos) || [])[1];
    const externo = /\bsrc\s*=/i.test(atributos);
    const inicioContenido = m.index + m[0].indexOf('>') + 1;
    scripts.push({
      externo,
      remoto: externo && /\bsrc\s*=\s*["']?\s*(https?:)?\/\//i.test(atributos),
      ejecutable: !externo && TIPOS_JS.has((tipo || '').toLowerCase()),
      modulo: (tipo || '').toLowerCase() === 'module',
      codigo: m[2],
      linea: html.slice(0, inicioContenido).split('\n').length,
    });
  }
  return scripts;
}

function lineaDelError(error) {
  const pila = String((error && error.stack) || '');
  const m = /revisor\.local\/:(\d+)(?::(\d+))?/.exec(pila) || /pagina\.html:(\d+)/.exec(pila);
  return m ? Number(m[1]) : null;
}

function revisarSintaxis(scripts) {
  const errores = [];
  for (const s of scripts) {
    if (!s.ejecutable) continue;
    try {
      new vm.Script(s.codigo, { filename: 'pagina.html', lineOffset: s.linea - 1 });
    } catch (error) {
      const linea = lineaDelError(error);
      const codigo = linea ? s.codigo.split('\n')[linea - s.linea] : '';
      errores.push({
        mensaje: `${error.name}: ${error.message}`,
        linea,
        codigo: recortar(codigo, 140),
      });
    }
  }
  return errores;
}

function ubicarEnHtml(scripts, lineaEnScript) {
  const ejecutables = scripts.filter((s) => s.ejecutable);
  if (!lineaEnScript || ejecutables.length !== 1) return { linea: null, codigo: '' };
  const s = ejecutables[0];
  return { linea: s.linea + lineaEnScript - 1, codigo: recortar(s.codigo.split('\n')[lineaEnScript - 1], 140) };
}

function cargarJsdom() {
  const candidatos = [path.join(__dirname, 'node_modules', 'jsdom'), process.env.REVISOR_JSDOM || ''];
  for (const c of candidatos) {
    if (!c) continue;
    try {
      return require(c);
    } catch (_) {
    }
  }
  return null;
}

function proxyInerte() {
  const fn = function () {
    return inerte;
  };
  const inerte = new Proxy(fn, {
    get(objetivo, clave) {
      if (clave === Symbol.toPrimitive) return () => 0;
      if (clave === 'then') return undefined;
      if (clave === 'width' || clave === 'height' || clave === 'length') return 0;
      if (clave === 'data') return new Uint8ClampedArray(4);
      return inerte;
    },
    set() {
      return true;
    },
    apply() {
      return inerte;
    },
    construct() {
      return inerte;
    },
  });
  return inerte;
}

function prepararVentana(window, conClic) {
  const nada = () => {};
  const consulta = (q) => ({
    matches: false,
    media: String(q),
    onchange: null,
    addListener: nada,
    removeListener: nada,
    addEventListener: nada,
    removeEventListener: nada,
    dispatchEvent: () => false,
  });
  const observador = class {
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  };
  const definir = (obj, clave, valor) => {
    try {
      Object.defineProperty(obj, clave, { value: valor, configurable: true, writable: true });
    } catch (_) {
    }
  };
  definir(window, 'matchMedia', consulta);
  for (const clave of ['scrollTo', 'scrollBy', 'scroll', 'print', 'alert', 'close', 'focus', 'blur', 'moveTo', 'resizeTo']) {
    definir(window, clave, nada);
  }
  definir(window, 'confirm', () => true);
  definir(window, 'prompt', () => null);
  definir(window, 'open', () => null);
  definir(window, 'fetch', () => new window.Promise(nada));
  definir(window, 'ResizeObserver', observador);
  definir(window, 'IntersectionObserver', observador);
  definir(window, 'PerformanceObserver', observador);
  if (window.URL) {
    definir(window.URL, 'createObjectURL', () => 'blob:revisor');
    definir(window.URL, 'revokeObjectURL', nada);
  }
  try {
    Object.defineProperty(window.navigator, 'clipboard', {
      value: { writeText: () => window.Promise.resolve(), readText: () => window.Promise.resolve('') },
      configurable: true,
    });
  } catch (_) {
  }
  const P = window.Element && window.Element.prototype;
  if (P) {
    definir(P, 'scrollIntoView', nada);
    definir(P, 'scrollTo', nada);
    definir(P, 'scrollBy', nada);
    definir(P, 'animate', () => ({
      finished: window.Promise.resolve(),
      cancel: nada,
      play: nada,
      pause: nada,
      finish: nada,
      reverse: nada,
      addEventListener: nada,
      onfinish: null,
    }));
    definir(P, 'requestFullscreen', () => window.Promise.resolve());
  }
  if (window.HTMLCanvasElement) {
    definir(window.HTMLCanvasElement.prototype, 'getContext', () => proxyInerte());
    definir(window.HTMLCanvasElement.prototype, 'toDataURL', () => 'data:,');
    definir(window.HTMLCanvasElement.prototype, 'toBlob', (cb) => cb && cb(null));
  }
  if (window.SVGElement) {
    const caja = () => ({ x: 0, y: 0, width: 0, height: 0 });
    definir(window.SVGElement.prototype, 'getBBox', caja);
    definir(window.SVGElement.prototype, 'getTotalLength', () => 0);
    definir(window.SVGElement.prototype, 'getComputedTextLength', () => 0);
    definir(window.SVGElement.prototype, 'getPointAtLength', () => ({ x: 0, y: 0 }));
    definir(window.SVGElement.prototype, 'getScreenCTM', () => null);
  }
  if (window.HTMLMediaElement) {
    definir(window.HTMLMediaElement.prototype, 'play', () => window.Promise.resolve());
    definir(window.HTMLMediaElement.prototype, 'pause', nada);
    definir(window.HTMLMediaElement.prototype, 'load', nada);
  }
  if (window.HTMLFormElement) {
    definir(window.HTMLFormElement.prototype, 'submit', nada);
    definir(window.HTMLFormElement.prototype, 'requestSubmit', nada);
  }
  if (window.EventTarget && conClic) {
    const original = window.EventTarget.prototype.addEventListener;
    definir(window.EventTarget.prototype, 'addEventListener', function (tipo, ...resto) {
      if (tipo === 'click' && this && this.nodeType === 1) conClic.add(this);
      return original.call(this, tipo, ...resto);
    });
  }
}

function describir(el) {
  const texto = recortar(el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '', 40);
  const id = el.id ? `#${el.id}` : '';
  return `${el.tagName.toLowerCase()}${id}${texto ? ` «${texto}»` : ''}`;
}

function clicables(document, conClic) {
  const selector = [
    '[onclick]',
    'button',
    '[role=tab]',
    '[role=button]',
    'a[href^="#"]',
    'summary',
    '[data-tab]',
    '[data-target]',
    '[tabindex]',
    'input[type=checkbox]',
    'input[type=radio]',
  ].join(',');
  const lista = Array.from(document.querySelectorAll(selector));
  for (const el of conClic) if (el.isConnected && !lista.includes(el)) lista.push(el);
  return lista.filter((el) => !el.disabled);
}

function revisar() {
  if (!ruta) return salir({ error: 'falta la ruta del HTML' });
  let html;
  try {
    html = fs.readFileSync(ruta, 'utf8');
  } catch (error) {
    return salir({ error: `no se pudo leer: ${error.message}` });
  }
  const scripts = scriptsEnLinea(html);
  const sintaxis = revisarSintaxis(scripts);
  if (sintaxis.length) return salir({ modo: 'sintaxis', errores_carga: sintaxis.slice(0, MAX_ERRORES), errores_clic: [] });
  const jsdom = cargarJsdom();
  if (!jsdom) return salir({ modo: 'sintaxis', errores_carga: [], errores_clic: [], sin_navegador: true });
  if (scripts.some((s) => s.remoto)) {
    return salir({ modo: 'sintaxis', errores_carga: [], errores_clic: [], scripts_remotos: true });
  }
  if (!scripts.some((s) => s.ejecutable) && !/\son[a-z]+\s*=/i.test(html)) {
    return salir({ modo: 'jsdom', errores_carga: [], errores_clic: [], sin_scripts: true });
  }

  const errores = [];
  const vistos = new Set();
  let fase = 'carga';
  let elementoActual = '';
  const consola = new jsdom.VirtualConsole();
  consola.on('jsdomError', (error) => {
    const causa = error && (error.cause || error.detail);
    const mensaje = causa && causa.message ? `${causa.name || 'Error'}: ${causa.message}` : String((error && error.message) || error);
    if (IGNORADOS.test(mensaje)) return;
    const clave = `${fase}|${mensaje}`;
    if (vistos.has(clave)) return;
    vistos.add(clave);
    const ubicacion = ubicarEnHtml(scripts, lineaDelError(causa || error));
    errores.push({ fase, mensaje: recortar(mensaje, 200), linea: ubicacion.linea, codigo: ubicacion.codigo, elemento: fase === 'clic' ? elementoActual : '' });
  });
  process.on('unhandledRejection', () => {});
  process.on('uncaughtException', () => {});

  const conClic = new Set();
  let dom;
  try {
    dom = new jsdom.JSDOM(html, {
      runScripts: 'dangerously',
      pretendToBeVisual: true,
      url: 'https://revisor.local/',
      virtualConsole: consola,
      beforeParse(window) {
        prepararVentana(window, conClic);
      },
    });
  } catch (error) {
    return salir({ modo: 'jsdom', errores_carga: [{ fase: 'carga', mensaje: recortar(error.message, 200), linea: null, codigo: '' }], errores_clic: [] });
  }
  const { window } = dom;
  const document = window.document;

  const resultado = () => ({
    modo: 'jsdom',
    errores_carga: errores.filter((e) => e.fase === 'carga').slice(0, MAX_ERRORES),
    errores_clic: errores.filter((e) => e.fase === 'clic').slice(0, MAX_ERRORES),
    clics,
    con_onclick: conOnclick,
    sin_efecto: sinEfecto,
    ejemplo_sin_efecto: ejemploSinEfecto,
  });
  let clics = 0;
  let conOnclick = 0;
  let sinEfecto = 0;
  let ejemploSinEfecto = '';

  const hacerClics = () => {
    fase = 'clic';
    const hechos = new Set();
    const fin = Date.now() + PLAZO_CLICS_MS;
    for (let ronda = 0; ronda < 4 && clics < MAX_CLICS && Date.now() < fin; ronda++) {
      const pendientes = clicables(document, conClic).filter((el) => !hechos.has(el));
      if (!pendientes.length) break;
      for (const el of pendientes) {
        if (clics >= MAX_CLICS || Date.now() >= fin) break;
        if (!el.isConnected) continue;
        hechos.add(el);
        elementoActual = describir(el);
        clics++;
        const medir = (el.hasAttribute('onclick') || conClic.has(el)) && !el.hasAttribute('disabled');
        const antes = medir ? document.body.innerHTML : null;
        try {
          el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
        } catch (_) {
        }
        if (medir) {
          conOnclick++;
          if (el.isConnected && document.body.innerHTML === antes) {
            sinEfecto++;
            if (!ejemploSinEfecto) ejemploSinEfecto = elementoActual;
          }
        }
      }
    }
    for (const entrada of Array.from(document.querySelectorAll('input[type=search], input[type=text], input:not([type])'))) {
      if (Date.now() >= fin) break;
      elementoActual = describir(entrada);
      for (const valor of ['a', '']) {
        try {
          entrada.value = valor;
          entrada.dispatchEvent(new window.Event('input', { bubbles: true }));
          entrada.dispatchEvent(new window.KeyboardEvent('keyup', { bubbles: true, key: 'a' }));
        } catch (_) {
        }
      }
    }
    for (const lista of Array.from(document.querySelectorAll('select'))) {
      if (Date.now() >= fin) break;
      elementoActual = describir(lista);
      for (let i = 0; i < Math.min(lista.options.length, 8); i++) {
        try {
          lista.selectedIndex = i;
          lista.dispatchEvent(new window.Event('change', { bubbles: true }));
        } catch (_) {
        }
      }
    }
    setTimeout(() => {
      try {
        window.close();
      } catch (_) {
      }
      salir(resultado());
    }, 200);
  };

  let empezado = false;
  const empezar = () => {
    if (empezado) return;
    empezado = true;
    setTimeout(hacerClics, 300);
  };
  window.addEventListener('load', empezar);
  setTimeout(empezar, 2500);
  setTimeout(() => salir({ ...resultado(), plazo_agotado: true }), PLAZO_TOTAL_MS).unref();
}

revisar();
