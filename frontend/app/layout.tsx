import type {Metadata} from 'next';
import './globals.css';
export const metadata:Metadata={title:'序场 · 剪辑工作台',description:'可恢复的本地短剧剪辑工程工作台'};
export default function RootLayout({children}:{children:React.ReactNode}){return <html lang="zh-CN"><body>{children}</body></html>}
