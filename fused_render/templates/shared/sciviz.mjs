/* ===========================================================================
 * sciViz core + UI kit — shared by the sci preview templates
 * (geotiff, netcdf, and zarr). Ported 1:1 from app/src/ui/components/sci/sciViz.ts +
 * SciPreviewParts.tsx / SciHistogram.tsx / useCanvasHover.ts.
 *
 * Extracted (2026-07) after confirming, function-by-function, that the
 * templates carried byte-identical copies of everything below. What did NOT
 * match stayed in-template: CF-time labeling (`cfTimeLabel` / `CF_TIME_MS`)
 * and the leading-dimension slider (`makeDimSlider`) are netcdf-side only
 * (geotiff has no CF-time axis or leading dims); the RGB composite helpers
 * (`drawRgb`, `stretchToBytes`) are geotiff-only.
 *
 * Pure helpers: colormap LUTs, stretch, stats, histogram, canvas draw, number
 * formatting, object-contain hit test. No DOM side effects beyond the small
 * `el`-based UI kit. No transport.
 * ======================================================================== */
export function clampByte(v){ if(v<=0) return 0; if(v>=255) return 255; return Math.round(v); }
const VIRIDIS_LUT = (()=>{
  const C=[
    [0.2777273272234177,0.005407344544966578,0.3340998053353061],
    [0.1050930431085774,1.404613529898575,1.384590162594685],
    [-0.3308618287255563,0.214847559468213,0.09509516302823659],
    [-4.634230498983486,-5.799100973351585,-19.33244095627987],
    [6.228269936347081,14.17993336680509,56.69055260068105],
    [4.776384997670288,-13.74514537774601,-65.35303263337234],
    [-5.435455855934631,4.645852612178535,26.3124352495832],
  ];
  const lut=new Uint8Array(256*3);
  for(let i=0;i<256;i++){ const t=i/255; let r=0,g=0,b=0;
    for(let k=C.length-1;k>=0;k--){ r=C[k][0]+t*r; g=C[k][1]+t*g; b=C[k][2]+t*b; }
    lut[i*3]=clampByte(r*255); lut[i*3+1]=clampByte(g*255); lut[i*3+2]=clampByte(b*255);
  }
  return lut;
})();
function buildAnchorLut(anchors){
  const lut=new Uint8Array(256*3); const spans=anchors.length-1;
  for(let i=0;i<256;i++){ const t=(i/255)*spans; const k=Math.min(spans-1,Math.floor(t)); const f=t-k;
    for(let c=0;c<3;c++) lut[i*3+c]=clampByte(anchors[k][c]+f*(anchors[k+1][c]-anchors[k][c]));
  }
  return lut;
}
const MAGMA_LUT = buildAnchorLut([[0,0,4],[29,17,71],[81,18,124],[130,38,129],[182,54,121],[230,81,100],[251,136,97],[254,194,135],[252,253,191]]);
const GRAY_LUT = (()=>{ const lut=new Uint8Array(256*3); for(let i=0;i<256;i++) lut[i*3]=lut[i*3+1]=lut[i*3+2]=i; return lut; })();
export const COLORMAPS = { viridis:VIRIDIS_LUT, magma:MAGMA_LUT, gray:GRAY_LUT };

function isValid(v,nodata){ return Number.isFinite(v) && (nodata==null || v!==nodata); }
export function percentileStretch(values, opts={}){
  const lo=opts.lo??2, hi=opts.hi??98, n=values.length;
  const step=n>200000?Math.ceil(n/100000):1; const finite=[];
  for(let i=0;i<n;i+=step){ const v=values[i]; if(isValid(v,opts.nodata)) finite.push(v); }
  if(finite.length===0) return {min:0,max:1};
  finite.sort((a,b)=>a-b);
  const at=(p)=>finite[Math.min(finite.length-1,Math.max(0,Math.round((p/100)*(finite.length-1))))];
  const min=at(lo), max=at(hi);
  return max>min?{min,max}:{min,max:min+1};
}
export function minMaxStretch(values, opts={}){
  let min=Infinity,max=-Infinity;
  for(let i=0;i<values.length;i++){ const v=values[i]; if(!isValid(v,opts.nodata)) continue; if(v<min)min=v; if(v>max)max=v; }
  if(!Number.isFinite(min)||!Number.isFinite(max)) return {min:0,max:1};
  return max>min?{min,max}:{min,max:min+1};
}
export function computeStretch(values, mode, opts={}){ return mode==="minmax"?minMaxStretch(values,opts):percentileStretch(values,opts); }
const SAMPLE_CAP=4000000;
function computeStats(values, opts={}){
  const n=values.length; const step=n>SAMPLE_CAP?Math.ceil(n/SAMPLE_CAP):1;
  let min=Infinity,max=-Infinity,sum=0,sumSq=0,valid=0,invalid=0;
  for(let i=0;i<n;i+=step){ const v=values[i];
    if(!isValid(v,opts.nodata)){ invalid++; continue; }
    valid++; if(v<min)min=v; if(v>max)max=v; sum+=v; sumSq+=v*v;
  }
  const sampled=valid+invalid;
  const validCount=step===1||sampled===0?valid:Math.round((valid/sampled)*n);
  const nanCount=n-validCount;
  if(valid===0) return {min:NaN,max:NaN,mean:NaN,std:NaN,validCount:0,nanCount:n};
  const mean=sum/valid; const variance=Math.max(0,sumSq/valid-mean*mean);
  return {min,max,mean,std:Math.sqrt(variance),validCount,nanCount};
}
function computeHistogram(values, bins, opts){
  const counts=new Uint32Array(Math.max(1,bins)); const {min,max}=opts; const span=max-min;
  const n=values.length; const step=n>SAMPLE_CAP?Math.ceil(n/SAMPLE_CAP):1;
  for(let i=0;i<n;i+=step){ const v=values[i]; if(!isValid(v,opts.nodata)) continue;
    const idx=span>0?Math.floor(((v-min)/span)*counts.length):0;
    counts[idx<0?0:idx>=counts.length?counts.length-1:idx]++;
  }
  return counts;
}
function canvasPixelAt(offsetX,offsetY,boxW,boxH,imgW,imgH){
  if(imgW<=0||imgH<=0||boxW<=0||boxH<=0) return null;
  const scale=Math.min(boxW/imgW,boxH/imgH);
  const x=Math.floor((offsetX-(boxW-imgW*scale)/2)/scale);
  const y=Math.floor((offsetY-(boxH-imgH*scale)/2)/scale);
  if(x<0||y<0||x>=imgW||y>=imgH) return null;
  return {x,y};
}
export function fmtNum(v){
  if(!Number.isFinite(v)) return String(v);
  if(Number.isInteger(v)&&Math.abs(v)<1e9) return String(v);
  const a=Math.abs(v);
  if(a!==0&&(a>=1e6||a<1e-4)) return v.toExponential(2);
  return String(Number(v.toPrecision(5)));
}
export function bucket(v,s){ const t=(v-s.min)/(s.max-s.min); return clampByte(t*255); }
export function drawHeatmap(canvas,data,width,height,stretch,opts={}){
  const ctx=canvas.getContext("2d"); if(!ctx) return;
  const lut=opts.lut??VIRIDIS_LUT;
  canvas.width=width; canvas.height=height;
  const img=ctx.createImageData(width,height); const out=img.data;
  for(let y=0;y<height;y++){ const srcRow=opts.flipY?height-1-y:y;
    for(let x=0;x<width;x++){ const v=data[srcRow*width+x]; const o=(y*width+x)*4;
      if(!isValid(v,opts.nodata)){ out[o+3]=0; continue; }
      const b=bucket(v,stretch)*3; out[o]=lut[b]; out[o+1]=lut[b+1]; out[o+2]=lut[b+2]; out[o+3]=255;
    }
  }
  ctx.putImageData(img,0,0);
}

/* ===========================================================================
 * UI kit — plain-DOM equivalents of SciPreviewParts.tsx / SciHistogram.tsx /
 * useCanvasHover.ts. `el` is a tiny hyperscript helper.
 * ======================================================================== */
export function el(tag, attrs={}, ...kids){
  const n=document.createElement(tag);
  for(const [k,v] of Object.entries(attrs)){
    if(v==null) continue;
    if(k==="class") n.className=v;
    else if(k==="text") n.textContent=v;
    else if(k==="html") n.innerHTML=v;
    else if(k==="style"&&typeof v==="object") Object.assign(n.style,v);
    else if(k.startsWith("on")&&typeof v==="function") n.addEventListener(k.slice(2).toLowerCase(),v);
    else n.setAttribute(k,v);
  }
  for(const kid of kids.flat()){ if(kid==null||kid===false) continue; n.append(kid.nodeType?kid:document.createTextNode(String(kid))); }
  return n;
}
export function statusEl(kind,msg){ return el("p",{class:"status"+(kind==="error"?" error":"")}, msg); }
export function metaList(rows){
  const shown=rows.filter(([,v])=>v!=null && v!=="");
  if(shown.length===0) return null;
  const dl=el("dl",{class:"meta"});
  for(const [k,v] of shown) dl.append(el("dt",{},k), el("dd",{}, v));
  return dl;
}
export function collapsible(label, child, open=false){
  const d=el("details",{class:"collapsible"}); if(open) d.setAttribute("open","");
  d.append(el("summary",{},label), el("div",{class:"body"}, child));
  return d;
}
const JSON_TOKEN=/"(?:\\.|[^"\\])*"|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[{}\[\],:]/g;
export function jsonBlock(value){
  let text; try{ text=JSON.stringify(value,(k,v)=>typeof v==="bigint"?v.toString():v,2); }catch{ text=String(value); }
  const pre=el("pre",{class:"json"}); if(!text){ pre.append("(none)"); return pre; }
  let last=0, m; JSON_TOKEN.lastIndex=0; // syntax-highlight: each token → text-only span (never innerHTML with raw values) ⇒ XSS-safe
  while((m=JSON_TOKEN.exec(text))){
    if(m.index>last) pre.append(document.createTextNode(text.slice(last,m.index)));
    const tok=m[0]; let cls;
    if(tok[0]==='"') cls=text[JSON_TOKEN.lastIndex]===":"?"j-key":"j-str"; // a string immediately before ':' is an object key
    else if(tok==="true"||tok==="false") cls="j-bool";
    else if(tok==="null") cls="j-null";
    else if(tok.length===1&&"{}[],:".includes(tok)) cls="j-punc";
    else cls="j-num";
    pre.append(el("span",{class:cls}, tok));
    last=JSON_TOKEN.lastIndex;
  }
  if(last<text.length) pre.append(document.createTextNode(text.slice(last)));
  return pre;
}
export function tinySelect(label, value, options, onChange){
  const sel=el("select");
  for(const [v,t] of options){ const o=el("option",{value:v},t); if(v===value) o.selected=true; sel.append(o); }
  sel.addEventListener("change",()=>onChange(sel.value));
  return el("label",{class:"field"}, label, sel);
}
/* Compact histogram + band-stats strip (ports SciHistogram). Colormap toggle
 * only repaints (stats/hists cached on the band arrays); log toggle rescales Y. */
export function makeHistogram(){
  const BINS=256, HEIGHT=120;
  let logY=false, current=null, lastColormap="viridis", hoverBin=-1;
  const canvas=el("canvas",{class:"hist"});
  const logBtn=el("button",{class:"log-btn",type:"button",title:"Toggle logarithmic Y axis","aria-pressed":"false"},"log");
  const loSpan=el("span",{class:"mono"}), hiSpan=el("span",{class:"mono"});
  const statsBox=el("div");
  const tip=el("div",{class:"hist-tip",hidden:""});
  const canvasWrap=el("div",{class:"hist-canvas-wrap"}, canvas, tip);
  const body=el("div",{}, el("div",{class:"hist-toolbar"}, logBtn), canvasWrap, el("div",{class:"hist-axis"}, loSpan, hiSpan), statsBox);
  const details=collapsible("Histogram", body, true);
  function compute(bands){
    const stats=bands.map(b=>computeStats(b.data,{nodata:b.nodata}));
    let lo=Infinity,hi=-Infinity;
    for(const s of stats){ if(s.validCount>0){ if(s.min<lo)lo=s.min; if(s.max>hi)hi=s.max; } }
    if(!Number.isFinite(lo)){ lo=0; hi=1; } else if(hi<=lo){ hi=lo+1; }
    const hists=bands.map(b=>computeHistogram(b.data,BINS,{min:lo,max:hi,nodata:b.nodata}));
    return {stats,hists,lo,hi};
  }
  function paint(){
    if(!current) return;
    const ctx=canvas.getContext("2d"); if(!ctx) return;
    canvas.width=BINS; canvas.height=HEIGHT; ctx.clearRect(0,0,BINS,HEIGHT);
    let maxCount=1; for(const h of current.hists) for(let i=0;i<h.length;i++) if(h[i]>maxCount) maxCount=h[i];
    const norm=c=> logY? Math.log1p(c)/Math.log1p(maxCount) : c/maxCount;
    const lut=COLORMAPS[lastColormap];
    current.hists.forEach((hist,bi)=>{
      const color=current.bands[bi]?.color;
      if(color){ ctx.globalCompositeOperation="screen"; ctx.fillStyle=color; }
      else ctx.globalCompositeOperation="source-over";
      for(let i=0;i<hist.length;i++){ if(hist[i]===0) continue;
        if(!color){ const b=Math.round((i/(hist.length-1))*255)*3; ctx.fillStyle=`rgb(${lut[b]},${lut[b+1]},${lut[b+2]})`; }
        const h=Math.max(1,Math.round(norm(hist[i])*HEIGHT));
        ctx.fillRect(i,HEIGHT-h,1,h);
      }
    });
    ctx.globalCompositeOperation="source-over";
    if(hoverBin>=0&&hoverBin<BINS){ ctx.fillStyle="rgba(255,255,255,0.18)"; ctx.fillRect(hoverBin,0,1,HEIGHT); } // highlight hovered bin column
  }
  function renderStats(){
    statsBox.replaceChildren();
    current.stats.forEach((s,i)=>{
      const band=current.bands[i]; const p=el("p",{class:"stat-line"});
      if(band?.label){ const sp=el("span",{},band.label+" "); if(band.color) sp.style.color=band.color; p.append(sp); }
      if(s.validCount===0) p.append("no valid samples");
      else p.append(`min ${fmtNum(s.min)} · max ${fmtNum(s.max)} · mean ${fmtNum(s.mean)} · σ ${fmtNum(s.std)} · valid ${s.validCount.toLocaleString()} px`);
      statsBox.append(p);
    });
  }
  logBtn.addEventListener("click",()=>{ logY=!logY; logBtn.setAttribute("aria-pressed",String(logY)); paint(); });
  function binAt(clientX){ const rect=canvas.getBoundingClientRect(); if(rect.width<=0) return -1; const b=Math.floor(((clientX-rect.left)/rect.width)*BINS); return b<0?0:b>=BINS?BINS-1:b; }
  function showTip(clientX){
    if(!current){ tip.hidden=true; return; }
    const bin=binAt(clientX); if(bin<0){ hideTip(); return; }
    hoverBin=bin; paint(); // repaint so the hovered column highlight follows the cursor
    const span=current.hi-current.lo, lo=current.lo+(bin/BINS)*span, hi=current.lo+((bin+1)/BINS)*span;
    tip.replaceChildren(el("div",{class:"rng"}, `${fmtNum(lo)}–${fmtNum(hi)}`)); // range = bin's value interval
    const multi=current.bands.length>1;
    current.hists.forEach((h,i)=>{ const band=current.bands[i]; const row=el("div",{class:"row"}); // raw counts per band (not log-scaled)
      if(multi&&band?.color) row.append(el("span",{class:"sw",style:{background:band.color}}));
      row.append(el("span",{}, `${band?.label?band.label+" ":"count "}${h[bin].toLocaleString()}`)); tip.append(row); });
    tip.hidden=false;
    const rect=canvas.getBoundingClientRect(); const px=((bin+0.5)/BINS)*rect.width; const tw=tip.offsetWidth;
    let left=px+10; if(left+tw>rect.width) left=px-10-tw; if(left<0) left=0; tip.style.left=`${left}px`; // flip left near the right edge, clamp at 0
  }
  function hideTip(){ if(hoverBin===-1&&tip.hidden) return; hoverBin=-1; tip.hidden=true; paint(); }
  canvas.addEventListener("mousemove",(e)=>showTip(e.clientX));
  canvas.addEventListener("mouseleave",hideTip);
  return {
    element: details,
    update(bands, colormap){ lastColormap=colormap; current={...compute(bands), bands}; loSpan.textContent=fmtNum(current.lo); hiSpan.textContent=fmtNum(current.hi); paint(); renderStats(); },
    setColormap(colormap){ lastColormap=colormap; paint(); },
  };
}
/* rAF-throttled canvas hover → nearest data-pixel indices (ports useCanvasHover). */
export function attachHover(canvas, getSize, onPixel){
  let raf=null;
  canvas.addEventListener("mousemove",(e)=>{
    if(raf!=null) return;
    const {clientX,clientY}=e;
    raf=requestAnimationFrame(()=>{ raf=null;
      const rect=canvas.getBoundingClientRect(); const {w,h}=getSize();
      onPixel(canvasPixelAt(clientX-rect.left,clientY-rect.top,rect.width,rect.height,w,h));
    });
  });
  canvas.addEventListener("mouseleave",()=>{ if(raf!=null) cancelAnimationFrame(raf); raf=null; onPixel(null); });
}
