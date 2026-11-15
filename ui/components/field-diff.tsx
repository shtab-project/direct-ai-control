export default function FieldDiff({before,after}:{before:unknown;after:unknown}){
 const a=(before&&typeof before==='object'?before:{Значение:before}) as Record<string,unknown>;
 const b=(after&&typeof after==='object'?after:{Значение:after}) as Record<string,unknown>;
 return <div className="table-wrap"><table><thead><tr><th>Поле</th><th>Было</th><th>Станет</th></tr></thead><tbody>{[...new Set([...Object.keys(a),...Object.keys(b)])].map(key=>{const old=JSON.stringify(a[key]??null,null,2),next=JSON.stringify(b[key]??null,null,2),changed=old!==next;return <tr key={key}><td>{key}</td><td><pre>{changed?<del>{old}</del>:old}</pre></td><td><pre>{changed?<ins>{next}</ins>:next}</pre></td></tr>;})}</tbody></table></div>;
}
