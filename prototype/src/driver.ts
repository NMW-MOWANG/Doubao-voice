import { useEffect, useRef, useState } from 'react';

/** 和真实程序里 overlay.show(state, ...) 的 state 对齐，另加一个 done 用来预览收尾。 */
export type Phase = 'idle' | 'recording' | 'recognizing' | 'done' | 'error';

/** 三个驱动源：假数据 / 从 bridge.py 推 / 本地麦克风（仅原型用来试听）。 */
export type Source = 'sim' | 'bridge' | 'mic';

export interface DriverFrame {
  phase: Phase;
  level: number;
  text: string;
}

export const PHASE_TEXT: Record<Phase, string> = {
  idle: '',
  recording: '正在录音',
  recognizing: '识别中…',
  done: '已识别',
  error: '识别出错',
};

const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v);

/** 模拟一次说话：把音节包络叠上去，让音量看着像人声而不是正弦波。 */
function speechEnvelope(t: number): number {
  const syllable = 0.5 + 0.5 * Math.sin(t * 11.0);
  const phrase = Math.pow(0.5 + 0.5 * Math.sin(t * 1.7 + 0.4), 1.6);
  const grain = 0.85 + 0.15 * Math.sin(t * 37.0);
  const noise = 0.92 + 0.08 * Math.random();
  return clamp01(phrase * (0.22 + 0.78 * syllable) * grain * noise);
}

/** 一次「待机 → 录音 → 识别 → 完成」的循环，时长按真实手感给。 */
const SCRIPT: ReadonlyArray<{ phase: Phase; ms: number }> = [
  { phase: 'idle', ms: 1200 },
  { phase: 'recording', ms: 3800 },
  { phase: 'recognizing', ms: 2200 },
  { phase: 'done', ms: 1400 },
];

export interface Driver {
  phase: Phase;
  text: string;
  /** 每帧被 voice-glow 采样的音量，故意走 ref：不触发 React 重渲染。 */
  levelRef: React.MutableRefObject<number>;
  /** bridge 的连接状态；null 表示当前不是 bridge 驱动。 */
  connected: boolean | null;
}

/**
 * 驱动源。level 走 ref 是为了配合 voice-glow 的 level={() => ...} 取数器：
 * 它每帧读一次、但不重渲染组件树，所以音量不能放在 state 里。
 */
export function useDriver(source: Source): Driver {
  const levelRef = useRef(0);
  const [phase, setPhase] = useState<Phase>('idle');
  const [text, setText] = useState('');
  const [connected, setConnected] = useState<boolean | null>(null);

  // ---- 假数据：自带动画，不开后台也能看效果 ----
  useEffect(() => {
    if (source !== 'sim') return;

    let index = 0;
    let stepStart = performance.now();
    let raf = requestAnimationFrame(function tick() {
      const now = performance.now();
      const step = SCRIPT[index];
      const elapsed = (now - stepStart) / 1000;

      if (now - stepStart >= step.ms) {
        index = (index + 1) % SCRIPT.length;
        stepStart = now;
        const next = SCRIPT[index];
        levelRef.current = 0;
        setPhase(next.phase);
        setText(PHASE_TEXT[next.phase]);
      } else {
        levelRef.current = step.phase === 'recording' ? speechEnvelope(elapsed) : 0;
      }
      raf = requestAnimationFrame(tick);
    });

    levelRef.current = 0;
    setPhase(SCRIPT[0].phase);
    setText(PHASE_TEXT[SCRIPT[0].phase]);
    return () => cancelAnimationFrame(raf);
  }, [source]);

  // ---- bridge：从 bridge.py 的 SSE 流拿真实(或模拟的)状态 ----
  useEffect(() => {
    if (source !== 'bridge') {
      setConnected(null);
      return;
    }

    const events = new EventSource('/events');
    events.onopen = () => setConnected(true);
    events.onerror = () => setConnected(false);
    events.onmessage = (ev) => {
      try {
        const frame = JSON.parse(ev.data) as DriverFrame;
        levelRef.current = typeof frame.level === 'number' ? clamp01(frame.level) : 0;
        if (frame.phase) setPhase(frame.phase);
        if (typeof frame.text === 'string') setText(frame.text);
      } catch {
        /* 忽略坏帧 */
      }
    };
    return () => events.close();
  }, [source]);

  return { phase, text, levelRef, connected };
}
