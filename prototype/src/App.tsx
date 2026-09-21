import { useEffect, useRef, useState } from 'react';
import VoiceBeam, { useMicrophone } from 'voice-glow';
import type { VoiceBeamColorVariant, VoiceBeamType } from 'voice-glow';
import { MetalFx, isMetalFxSupported } from 'metal-fx';
import type { MetalFxPreset } from 'metal-fx';
import { PHASE_TEXT, useDriver } from './driver';
import type { Phase, Source } from './driver';

/** 跟 overlay.py 的 STATE_COLORS 对齐，方便和现在的浮标直接对比。 */
const STATE_DOT: Record<Phase, string> = {
  idle: '#6b7280',
  recording: '#e8453c',
  recognizing: '#f5a623',
  done: '#34a853',
  error: '#d93025',
};

/** 和 overlayd.py 里 WIDTH, HEIGHT 一致——原型就在这个真实尺寸里看效果。 */
const PILL_W = 340;
const PILL_H = 60;

const VARIANTS: VoiceBeamColorVariant[] = [
  'colorful',
  'ocean',
  'sunset',
  'forest',
  'candy',
  'ice',
  'gold',
  'mono',
];

const TYPES: VoiceBeamType[] = ['default', 'pill', 'mobile'];

/** metal-fx 的三个配色预设。 */
const METAL_PRESETS: MetalFxPreset[] = ['chromatic', 'silver', 'gold'];

function Slider(props: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  digits?: number;
  onChange: (value: number) => void;
}) {
  const { label, value, min, max, step, digits = 2, onChange } = props;
  return (
    <label className="ctl">
      <span className="ctl-label">
        {label}
        <b>{value.toFixed(digits)}</b>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
      />
    </label>
  );
}

function Meter(props: { label: string; value: number }) {
  return (
    <div className="meter">
      <span className="meter-label">{props.label}</span>
      <span className="meter-track">
        <i style={{ width: `${Math.round(props.value * 100)}%` }} />
      </span>
      <span className="meter-value">{props.value.toFixed(2)}</span>
    </div>
  );
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

export default function App() {
  const [source, setSource] = useState<Source>('sim');

  // 外观旋钮（只挑对这个尺寸真正有影响的几个）
  const [type, setType] = useState<VoiceBeamType>('default');
  const [variant, setVariant] = useState<VoiceBeamColorVariant>('colorful');
  const [scale, setScale] = useState(1);
  const [bend, setBend] = useState(28);
  const [reach, setReach] = useState(1.2);
  const [bandStrength, setBandStrength] = useState(1.55);
  const [distortion, setDistortion] = useState(0.62);
  const [strength, setStrength] = useState(1);

  // 响应旋钮：真实程序里 level 已经是 0–1 的 RMS，所以增益要收着点
  const [sensitivity, setSensitivity] = useState(1);
  const [threshold, setThreshold] = useState(0.02);
  const [idle, setIdle] = useState(0.15);

  const [showFrame, setShowFrame] = useState(true);

  // 麦克风圆圈上的 metal-fx（实时 WebGL 液体金属环）
  const [metal, setMetal] = useState(true);
  const [metalPreset, setMetalPreset] = useState<MetalFxPreset>('chromatic');
  const [metalStrength, setMetalStrength] = useState(0.9);
  const [innerShadow, setInnerShadow] = useState(true);
  const [metalGlow, setMetalGlow] = useState(true);
  const [normalizeHost, setNormalizeHost] = useState(true);
  const [reflectPill, setReflectPill] = useState(false);
  const [autoPause, setAutoPause] = useState(true);
  const [webglOk] = useState(() => isMetalFxSupported());
  const pillRef = useRef<HTMLDivElement>(null);

  const driver = useDriver(source);
  const mic = useMicrophone();

  useEffect(() => {
    if (source === 'mic') {
      void mic.start().catch(() => undefined);
    } else {
      mic.stop();
    }
    // mic 对象每次渲染都是新的，这里只关心 source 变化
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source]);

  // 读数：每 100ms 把两个 ref 抄进 state，避免每帧重渲染
  const beamRef = useRef(0);
  const [inLevel, setInLevel] = useState(0);
  const [beamLevel, setBeamLevel] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => {
      setInLevel(driver.levelRef.current);
      setBeamLevel(beamRef.current);
    }, 100);
    return () => window.clearInterval(id);
  }, [driver.levelRef]);

  const active = source === 'mic' ? mic.state === 'live' : driver.phase !== 'idle';
  const processing = source !== 'mic' && driver.phase === 'recognizing';
  const dot = STATE_DOT[driver.phase];
  const label =
    source === 'mic'
      ? mic.state === 'live'
        ? '麦克风已开'
        : `麦克风：${mic.state}`
      : driver.text || PHASE_TEXT[driver.phase];

  return (
    <div className="page">
      <header className="hero">
        <h1>voice-glow 原型</h1>
        <p>
          在浮标的真实尺寸 <code>340×60</code> 下预览。虚线框就是 overlayd.py 里那个 X11
          窗口的边界——框外的光在真实程序里会被裁掉。
        </p>
      </header>

      <section className="stage">
        <div className="pill-frame" style={{ width: PILL_W, height: PILL_H }}>
          <VoiceBeam
            type={type}
            theme="dark"
            colorVariant={variant}
            scale={scale}
            bend={bend}
            reach={reach}
            bandStrength={bandStrength}
            distortion={distortion}
            strength={strength}
            sensitivity={sensitivity}
            threshold={threshold}
            idle={idle}
            borderRadius={PILL_H / 2}
            active={active}
            processing={processing}
            stream={source === 'mic' ? mic.stream : null}
            level={() => driver.levelRef.current}
            onLevel={(level) => {
              beamRef.current = level;
            }}
          >
            <div className="pill" ref={pillRef}>
              {metal ? (
                <MetalFx
                  className="ring-metal"
                  variant="circle"
                  preset={metalPreset}
                  theme="dark"
                  strength={metalStrength}
                  innerShadow={innerShadow}
                  disableGlow={!metalGlow}
                  paused={autoPause && !active}
                  normalizeHostStyles={normalizeHost}
                  reflectionTargets={reflectPill ? [pillRef] : undefined}
                >
                  <span className="ring" style={{ color: dot }}>
                    <MicGlyph />
                  </span>
                </MetalFx>
              ) : (
                <span className="ring" style={{ color: dot }}>
                  <MicGlyph />
                </span>
              )}
              <span className="pill-text">{label || '（待机）'}</span>
            </div>
          </VoiceBeam>
          {showFrame && (
            <span className="frame-outline" style={{ borderRadius: PILL_H / 2 }}>
              <em>
                {PILL_W}×{PILL_H}
              </em>
            </span>
          )}
        </div>
      </section>

      <aside className="panel">
        <div className="panel-block">
          <h2>驱动</h2>
          <div className="buttons">
            {(
              [
                ['sim', '假数据'],
                ['bridge', '桥接'],
                ['mic', '麦克风'],
              ] as const
            ).map(([value, text]) => (
              <button
                key={value}
                className={source === value ? 'on' : ''}
                onClick={() => setSource(value)}
              >
                {text}
              </button>
            ))}
          </div>
          {source === 'bridge' && (
            <p className="hint">
              {driver.connected === true && '已连上 bridge.py 的 SSE 流'}
              {driver.connected === false && '连不上——先在另一个终端跑 python3 prototype/bridge.py'}
              {driver.connected === null && '连接中…'}
            </p>
          )}
          {source === 'mic' && (
            <p className="hint">
              直接吃本地麦克风（原型试用）。正式接入时不会用它——那会和 pw-record 抢麦，
              真实程序里改由 Python 的 recorder.level 推给 level。
            </p>
          )}
        </div>

        <div className="panel-block">
          <h2>外观</h2>
          <div className="seg">
            {TYPES.map((value) => (
              <button
                key={value}
                className={type === value ? 'on' : ''}
                onClick={() => setType(value)}
              >
                {value}
              </button>
            ))}
          </div>
          <label className="ctl">
            <span className="ctl-label">
              配色<b>{variant}</b>
            </span>
            <select value={variant} onChange={(e) => setVariant(e.target.value as VoiceBeamColorVariant)}>
              {VARIANTS.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <Slider label="scale 整体大小" value={scale} min={0.3} max={2} step={0.05} onChange={setScale} />
          <Slider label="bend 顶弧高度" value={bend} min={0} max={120} step={1} digits={0} onChange={setBend} />
          <Slider label="reach 长高倍率" value={reach} min={0.4} max={3} step={0.05} onChange={setReach} />
          <Slider label="bandStrength 光带" value={bandStrength} min={0} max={3} step={0.05} onChange={setBandStrength} />
          <Slider label="distortion 扭曲" value={distortion} min={0} max={1} step={0.02} onChange={setDistortion} />
          <Slider label="strength 整体透明度" value={strength} min={0} max={1} step={0.05} onChange={setStrength} />
        </div>

        <div className="panel-block">
          <h2>响应</h2>
          <Slider label="sensitivity 增益" value={sensitivity} min={0.5} max={5} step={0.1} digits={1} onChange={setSensitivity} />
          <Slider label="threshold 噪声门" value={threshold} min={0} max={0.2} step={0.005} digits={3} onChange={setThreshold} />
          <Slider label="idle 静默驻留" value={idle} min={0} max={0.5} step={0.01} onChange={setIdle} />
        </div>

        <div className="panel-block">
          <h2>金属环（麦克风圆圈）</h2>
          <label className="check">
            <input type="checkbox" checked={metal} onChange={(e) => setMetal(e.target.checked)} />
            启用 MetalFx
          </label>
          <p className="hint">
            WebGL2：{webglOk ? '支持' : '不支持——会退化成普通子元素（不会报错）'}
          </p>
          {metal && (
            <>
              <div className="seg">
                {METAL_PRESETS.map((value) => (
                  <button
                    key={value}
                    className={metalPreset === value ? 'on' : ''}
                    onClick={() => setMetalPreset(value)}
                  >
                    {value}
                  </button>
                ))}
              </div>
              <Slider
                label="strength 强度"
                value={metalStrength}
                min={0}
                max={1}
                step={0.05}
                onChange={setMetalStrength}
              />
              <label className="check">
                <input
                  type="checkbox"
                  checked={innerShadow}
                  onChange={(e) => setInnerShadow(e.target.checked)}
                />
                innerShadow 顶部内边缘光晕
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={metalGlow}
                  onChange={(e) => setMetalGlow(e.target.checked)}
                />
                游走光晕（disableGlow 取反）
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={normalizeHost}
                  onChange={(e) => setNormalizeHost(e.target.checked)}
                />
                normalizeHostStyles：吞掉原来那圈彩色边框
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={reflectPill}
                  onChange={(e) => setReflectPill(e.target.checked)}
                />
                reflectionTargets：让浮标本体接住金属反光
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={autoPause}
                  onChange={(e) => setAutoPause(e.target.checked)}
                />
                非激活时 paused（省 CPU）
              </label>
            </>
          )}
        </div>

        <div className="panel-block">
          <h2>读数</h2>
          <Meter label="输入 level" value={inLevel} />
          <Meter label="光束 level" value={beamLevel} />
          <label className="check">
            <input type="checkbox" checked={showFrame} onChange={(e) => setShowFrame(e.target.checked)} />
            显示 340×60 裁切框
          </label>
        </div>
      </aside>
    </div>
  );
}
