import React, { useEffect, useRef } from 'react';
import { Archive, FileText, Film, Image, Music2, File, X, Check, ArrowDownToLine, LoaderCircle } from 'lucide-react';
import { bytes } from './api.js';

export function Logo({ small = false }) {
  return <div className={`brand ${small ? 'brand-small' : ''}`}><span className="brand-mark"><ArrowDownToLine size={23} strokeWidth={2.5} /></span><span className="brand-word">ndm<span className="brand-period">.</span></span></div>;
}

export function FileIcon({ category, size = 21 }) {
  const Icon = { video: Film, audio: Music2, archive: Archive, image: Image, document: FileText }[category] || File;
  return <span className={`file-icon ${category}`}><Icon size={size} strokeWidth={1.7} /></span>;
}

export function Toggle({ checked, onChange, label, disabled = false }) {
  return <button type="button" role="switch" aria-checked={checked} aria-label={label} disabled={disabled} className={`toggle ${checked ? 'on' : ''}`} onClick={() => onChange(!checked)}><span /></button>;
}

export function Modal({ title, subtitle, children, onClose, wide = false }) {
  const ref = useRef(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement;
    const node = ref.current;
    const focusable = () => [...node.querySelectorAll('button:not(:disabled), input:not(:disabled), select, textarea, a[href], [tabindex="0"]')];
    const timer = setTimeout(() => (node.querySelector('[autofocus]') || focusable()[0])?.focus(), 30);
    const key = event => {
      if (event.key === 'Escape') closeRef.current();
      if (event.key === 'Tab') {
        const items = focusable();
        if (!items.length) return;
        if (event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items.at(-1).focus(); }
        else if (!event.shiftKey && document.activeElement === items.at(-1)) { event.preventDefault(); items[0].focus(); }
      }
    };
    document.addEventListener('keydown', key);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { clearTimeout(timer); document.removeEventListener('keydown', key); document.body.style.overflow = overflow; previous?.focus(); };
  }, []);
  return <div className="modal-backdrop" onMouseDown={e => e.target === e.currentTarget && onClose()}><section ref={ref} role="dialog" aria-modal="true" aria-labelledby="modal-title" className={`modal ${wide ? 'modal-wide' : ''}`}><header className="modal-header"><div><h2 id="modal-title">{title}</h2>{subtitle && <p>{subtitle}</p>}</div><button className="icon-button" aria-label="Close dialog" onClick={onClose}><X size={19} /></button></header>{children}</section></div>;
}

export function EmptyState({ icon: Icon = ArrowDownToLine, title, text, action }) {
  return <div className="empty-state"><span className="empty-icon"><Icon size={29} strokeWidth={1.5} /></span><h3>{title}</h3><p>{text}</p>{action}</div>;
}

export function SpeedChart({ values = [], large = false }) {
  const width = 280, height = large ? 130 : 105;
  const max = Math.max(...values, 1024 * 1024) * 1.18;
  const points = values.map((v, i) => [i / Math.max(1, values.length - 1) * width, height - 8 - v / max * (height - 20)]);
  const line = points.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
  const area = `${line} L${width},${height} L0,${height} Z`;
  return <div className={`speed-chart ${large ? 'large' : ''}`}><svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label={`Download speed over the last minute. Currently ${bytes(values.at(-1) || 0)} per second.`}><defs><linearGradient id={large ? 'chart-fill-large' : 'chart-fill'} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="var(--accent)" stopOpacity=".19" /><stop offset="100%" stopColor="var(--accent)" stopOpacity=".015" /></linearGradient></defs>{[0.2, 0.55, 0.9].map(y => <line key={y} x1="0" x2={width} y1={height * y} y2={height * y} stroke="var(--border)" strokeDasharray="3 4" />)}<path d={area} fill={`url(#${large ? 'chart-fill-large' : 'chart-fill'})`} /><path d={line} fill="none" stroke="var(--accent)" strokeWidth="2.2" strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" /></svg><div className="chart-axis"><span>60s ago</span><span>30s</span><span>Now</span></div></div>;
}

export function MiniSpark({ values }) {
  const max = Math.max(...values, 1);
  const points = values.map((v, i) => `${i / Math.max(1, values.length - 1) * 88},${28 - v / max * 24}`).join(' ');
  return <svg className="mini-spark" viewBox="0 0 90 32" aria-hidden="true"><polyline points={points} fill="none" stroke="var(--accent)" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>;
}

export function Status({ task }) {
  const labels = { downloading: task.phase === 'processing' ? 'Processing' : 'Downloading', queued: 'In queue', paused: 'Paused', completed: 'Completed', error: 'Needs attention' };
  return <span className={`status-pill ${task.status}`}><span className="status-dot" />{labels[task.status] || task.status}</span>;
}

export function Toasts({ items, dismiss }) {
  return <div className="toasts" aria-live="polite">{items.map(item => <div key={item.id} className={`toast ${item.tone}`}><span className="toast-icon">{item.tone === 'error' ? <X size={16} /> : <Check size={16} />}</span><span>{item.message}</span><button aria-label="Dismiss notification" onClick={() => dismiss(item.id)}><X size={14} /></button></div>)}</div>;
}

export function Loading() {
  return <div className="loading-screen"><Logo /><LoaderCircle className="spin" size={22} /><p>Getting your workspace ready…</p></div>;
}
