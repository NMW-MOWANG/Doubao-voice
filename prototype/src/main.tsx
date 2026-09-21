import { createRoot } from 'react-dom/client';
import App from './App';
import './styles.css';

// 不开 StrictMode：原型里它会把 EventSource / 麦克风副作用跑两遍，噪音多于价值。
createRoot(document.getElementById('root')!).render(<App />);
