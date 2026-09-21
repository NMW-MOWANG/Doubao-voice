/* 嵌入模式：只画浮标本体，整页透明，状态由宿主机（overlayd.py）推进来。
 *
 * 和预览页的区别：不加载预览页的面板/背景样式，只有一个 340×60 的胶囊；
 * 状态不走 SSE，而是暴露 window.voiceOverlay.apply()，吃的帧和 overlayd.py
 * 从 stdin 收到的一模一样（show / level / text / hide），所以 Python 那边
 * 只是把「画」这一步换成把帧转发给页面。
 *
 * 两条踩过的坑，别再踩回去：
 *  1) 浮标窗口在「拿到第一帧之前」是隐藏的，隐藏时页面视口是 0×0。所以胶囊尺寸
 *     必须写死 340×60（见 embed.html 的 .canvas），用百分比会被量成 0，而 0 尺寸的
 *     canvas 在 WebKit 里拿不到 WebGL context。
 *  2) 特效组件出错会把整棵 React 树卸掉（页面空白）。所以外面套了 EffectBoundary，
 *     并且挂 MetalFx 之前先用它自己那组参数探一次 WebGL2。
 */

import { Component, useEffect, useRef, useState } from 'react';
import type { ErrorInfo, ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import VoiceBeam from 'voice-glow';
import { MetalFx } from 'metal-fx';
import type { MetalFxPreset } from 'metal-fx';
import { PHASE_TEXT } from './driver';
import type { Phase } from './driver';
import './pill.css';

/** 跟 overlay.py / overlayd.py 的 STATE_COLORS 对齐。 */
const STATE_DOT: Record<Phase, string> = {
  idle: '#6b7280',
  recording: '#e8453c',
  recognizing: '#f5a623',
  done: '#34a853',
  error: '#d93025',
};

const PHASES: Phase[] = ['idle', 'recording', 'recognizing', 'done', 'error'];

/** 嵌入时的外观参数：和预览页默认值一致，改这里就是改真实浮标的样子。 */
const LOOK = {
  colorVariant: 'colorful',
  scale: 1,
  bend: 28,
  reach: 1.2,
  bandStrength: 1.55,
  distortion: 0.62,
  strength: 1,
  sensitivity: 1,
  threshold: 0.02,
  idle: 0.15,
  metal: true,
  metalPreset: 'chromatic' as MetalFxPreset,
  metalStrength: 0.9,
} as const;

interface Frame {
  cmd: string;
  state?: string;
  text?: string;
  value?: number;
}

const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v);
const isPhase = (v: unknown): v is Phase => typeof v === 'string' && PHASES.includes(v as Phase);

/** 用 metal-fx 自己那条分支探一次 WebGL2：它优先用 OffscreenCanvas，而 WebKitGTK 有
 *  OffscreenCanvas 却不支持在其上创建 WebGL2，于是它会抛错（它的 isMetalFxSupported()
 *  用普通 canvas 探，所以照样返回 true），别等它抛。 */
let metalProbe: boolean | null = null;
function metalUsable(): boolean {
  if (metalProbe !== null) return metalProbe;
  try {
    const size = 64;
    let gl: unknown;
    if (typeof OffscreenCanvas !== 'undefined') {
      gl = new OffscreenCanvas(size, size).getContext('webgl2', {
        alpha: true,
        premultipliedAlpha: true,
        antialias: false,
      });
    } else {
      const canvas = document.createElement('canvas');
      canvas.width = size;
      canvas.height = size;
      gl = canvas.getContext('webgl2', {
        alpha: true,
        premultipliedAlpha: true,
        antialias: false,
        preserveDrawingBuffer: true,
      });
    }
    metalProbe = !!gl;
  } catch {
    metalProbe = false;
  }
  return metalProbe;
}

/** 特效挂了也不能让浮标变空白——退化成普通胶囊，至少和 cairo 版一样能用。 */
class EffectBoundary extends Component<
  { fallback: ReactNode; children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: unknown, _info: ErrorInfo) {
    console.error('[voice-overlay] 特效组件出错，退回普通胶囊：', error);
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

function MicGlyph() {
  return (
    <svg viewBox="0 0 24 24" width="17" height="17" aria-hidden>
      <rect x="9.4" y="3.6" width="5.2" height="9.6" rx="2.6" fill="currentColor" />
      <path
        d="M6.2 11.4a5.8 5.8 0 0 0 11.6 0"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
      <path
        d="M12 17.2v3.2M9.2 20.4h5.6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  );
}

declare global {
  interface Window {
    voiceOverlay?: {
      apply: (frame: Frame) => void;
      debug: () => Record<string, unknown>;
    };
  }
}

function Overlay() {
  const levelRef = useRef(0);
  const beamRef = useRef(0);
  const [phase, setPhase] = useState<Phase>('idle');
  const [text, setText] = useState('');
  const [visible, setVisible] = useState(false);
  const latest = useRef({ phase, text, visible });
  const [metalOk] = useState(() => metalUsable());

  useEffect(() => {
    latest.current = { phase, text, visible };
  }, [phase, text, visible]);

  // 宿主接口：只在挂载时装一次，里面的 setState/ref 都是稳定的
  useEffect(() => {
    const apply = (frame: Frame) => {
      if (!frame || typeof frame.cmd !== 'string') return;
      switch (frame.cmd) {
        case 'show':
          levelRef.current = 0;
          setPhase(isPhase(frame.state) ? frame.state : 'recording');
          setText(typeof frame.text === 'string' ? frame.text : '');
          setVisible(true);
          break;
        case 'level':
          levelRef.current = clamp01(Number(frame.value) || 0);
          break;
        case 'text':
          setText(typeof frame.text === 'string' ? frame.text : '');
          break;
        case 'hide':
          levelRef.current = 0;
          setVisible(false);
          break;
      }
    };
    window.voiceOverlay = {
      apply,
      debug: () => ({
        ...latest.current,
        level: levelRef.current,
        beam: beamRef.current,
        webgl: metalOk,
      }),
    };
    // 页面就绪的握手：overlayd.py 监听标题，收到就把攒着的帧灌进来
    document.title = 'voice-overlay-ready';
  }, [metalOk]);

  const active = visible && phase !== 'idle';
  const processing = phase === 'recognizing';
  const dot = STATE_DOT[phase];
  const label = text || PHASE_TEXT[phase];

  // 隐藏时整棵特效树都不挂：voice-glow 的 rAF 循环是常驻的，页面一直开着也要占几个点 CPU，
  // 而浮标大部分时间是隐藏的。窗口本来就是隐藏的，这里返回空即可。
  if (!visible) {
    return <div className="canvas" />;
  }

  const plainPill = (
    <div className="pill">
      <span className="ring" style={{ color: dot }}>
        <MicGlyph />
      </span>
      <span className="pill-text">{label}</span>
    </div>
  );

  const plainRing = (
    <span className="ring" style={{ color: dot }}>
      <MicGlyph />
    </span>
  );

  // 金属环自己套一层边界：它要是挂了，不该把父层 VoiceBeam 的光束一起带下去
  const ring =
    LOOK.metal && metalOk ? (
      <EffectBoundary fallback={plainRing}>
        <MetalFx
          className="ring-metal"
          variant="circle"
          preset={LOOK.metalPreset}
          theme="dark"
          strength={LOOK.metalStrength}
          innerShadow
          paused={!active}
        >
          {plainRing}
        </MetalFx>
      </EffectBoundary>
    ) : (
      plainRing
    );

  const richPill = (
    <div className="pill">
      {ring}
      <span className="pill-text">{label}</span>
    </div>
  );

  return (
    <div className="canvas">
      <EffectBoundary fallback={plainPill}>
        <VoiceBeam
          type="default"
          theme="dark"
          colorVariant={LOOK.colorVariant}
          scale={LOOK.scale}
          bend={LOOK.bend}
          reach={LOOK.reach}
          bandStrength={LOOK.bandStrength}
          distortion={LOOK.distortion}
          strength={LOOK.strength}
          sensitivity={LOOK.sensitivity}
          threshold={LOOK.threshold}
          idle={LOOK.idle}
          borderRadius={30}
          active={active}
          processing={processing}
          level={() => levelRef.current}
          onLevel={(level) => {
            beamRef.current = level;
          }}
        >
          {richPill}
        </VoiceBeam>
      </EffectBoundary>
    </div>
  );
}

const host = document.getElementById('root');
if (host) {
  createRoot(host).render(<Overlay />);
}
