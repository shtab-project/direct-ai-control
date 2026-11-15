import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
export default function MarkdownAnswer({text}:{text:string}){return <div className="assistant-markdown"><Markdown remarkPlugins={[remarkGfm]} components={{img:()=> <em>Внешнее изображение не загружается</em>,a:({children,href})=><a href={href} target="_blank" rel="noreferrer noopener">{children}</a>}}>{text}</Markdown></div>;}
