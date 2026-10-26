// Lives only while this page is open. No service worker or remote messaging.
export class AlertEngine {
  constructor({seen=[],emit=()=>{},save=()=>{}}={}) {this.seen=new Set(seen);this.emit=emit;this.save=save;this.initial=true;this.wasEnabled=false;}
  scan(events,enabled) {
    const baseline=this.initial||!this.wasEnabled;
    const additions=[];
    for(const event of events){
      const key=event.key;
      if(this.seen.has(key))continue;
      this.seen.add(key);
      if(enabled&&!baseline){this.emit(event);additions.push(event);}
    }
    this.initial=false;this.wasEnabled=enabled;
    if(this.seen.size>1000)this.seen=new Set([...this.seen].slice(-1000));
    this.save([...this.seen]);return additions;
  }
}
export function playChime(context) {
  for(let i=0;i<2;i++) {
    const oscillator=context.createOscillator(),gain=context.createGain();
    oscillator.type='sine';oscillator.frequency.value=i?880:660;
    gain.gain.setValueAtTime(0,context.currentTime+i*.18);
    gain.gain.linearRampToValueAtTime(.12,context.currentTime+i*.18+.02);
    gain.gain.exponentialRampToValueAtTime(.001,context.currentTime+i*.18+.16);
    oscillator.connect(gain);gain.connect(context.destination);
    oscillator.start(context.currentTime+i*.18);oscillator.stop(context.currentTime+i*.18+.17);
  }
}
