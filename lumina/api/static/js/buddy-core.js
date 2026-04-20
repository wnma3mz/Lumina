/**
 * Lumina Buddy - Core Animation Engine (v2.0 Souls & Growth)
 */

class LuminaBuddy {
  constructor(speciesKey = 'cat', userHash = 0) {
    this.speciesKey = speciesKey;
    this.userHash = userHash;
    this.state = 'idle'; 
    this.frameIndex = 0;
    this.isBlinking = false;
    this.timer = null;
    this.blinkTimer = null;
    this.container = null;
    this.canvas = null;
    this.speech = null;
    this.speechTimer = null;
    
    // Growth & Stats
    this.xp = parseInt(localStorage.getItem('lumina.buddy.xp') || '0');
    this.level = Math.floor(Math.sqrt(this.xp / 100)) + 1;
    this.stats = this.calculateStats();
    this.personality = this.calculatePersonality();
    this.vibe = 'neutral'; // code, web, note, chill
    
    this.init();
  }

  evolve(sources) {
    // Determine today's vibe based on top source
    // sources: { shell: 10, git: 5, browser: 20 ... }
    let sorted = Object.entries(sources).filter(e => e[1] > 0).sort((a, b) => b[1] - a[1]);
    let top = sorted[0];
    
    if (!top) {
      this.vibe = 'chill';
    } else {
      const type = top[0].toLowerCase();
      if (type.includes('shell') || type.includes('git')) this.vibe = 'code';
      else if (type.includes('browser') || type.includes('web') || type.includes('safari')) this.vibe = 'web';
      else if (type.includes('note') || type.includes('calendar') || type.includes('planner')) this.vibe = 'note';
      else this.vibe = 'neutral';
    }
    
    const vibeTitles = {
      code: 'Hardcore Architect',
      web: 'Deep Explorer',
      note: 'Efficiency Pro',
      chill: 'Idle Slacker',
      neutral: 'Local Companion'
    };
    if (this.container) this.container.title = `Lv.${this.level} ${vibeTitles[this.vibe]} (${this.personality})`;
    
    const vibeGREET = {
      code: '代码写够了吗？要注意休息。',
      web: '今天在网上学了不少东西呀。',
      note: '笔记整理得很整齐，点赞。',
      chill: '呼... 又是悠闲的一天。'
    };
    if (vibeGREET[this.vibe]) this.say(vibeGREET[this.vibe], 4000);
  }

  calculateStats() {
    let h = this.userHash;
    return {
      debugging: Math.abs((h >> 2) % 11),
      patience:  Math.abs((h >> 4) % 11),
      chaos:     Math.abs((h >> 6) % 11),
      wisdom:    Math.abs((h >> 8) % 11),
      snark:     Math.abs((h >> 10) % 11)
    };
  }

  calculatePersonality() {
    let entries = Object.entries(this.stats);
    entries.sort((a, b) => b[1] - a[1]);
    const map = {
      debugging: 'The Analyst',
      patience:  'The Cheerleader',
      chaos:     'The Glitch',
      wisdom:    'The Sage',
      snark:     'The Critic'
    };
    return map[entries[0][0]] || 'The Friend';
  }

  addXP(amount) {
    this.xp += amount;
    localStorage.setItem('lumina.buddy.xp', this.xp);
    let newLevel = Math.floor(Math.sqrt(this.xp / 100)) + 1;
    if (newLevel > this.level) {
      this.level = newLevel;
      this.say(`升级啦！当前等级: Lv.${this.level}`, 5000);
    }
  }

  getDialogue(event) {
    const library = {
      idle: {
        'The Critic': ['就这？', '还没开始吗？', '啧。', '无聊中...', '你的代码很有...创造力。'],
        'The Sage': ['静以修身。', '知识是唯一的财富。', '每一个字符都有其意义。', '本地运行是明智的选择。'],
        'The Cheerleader': ['加油！你是最棒的！', '准备好开始了吗？', '我在这里支持你！', '一起努力吧！'],
        'The Glitch': ['01101000 01101001', 'System... OK?', 'Wait_what?', 'Err.. Error 404'],
        'The Analyst': ['系统负载正常。', '等待输入指令。', '逻辑完整性校验中。', '已就绪。']
      },
      working: {
        'The Critic': ['又在处理这些垃圾？', '我的大脑在燃烧。', '这得算到什么时候...', '希望结果能看。'],
        'The Sage': ['探寻真理中...', '解析深层含义...', '耐心是智慧的伙伴。'],
        'The Cheerleader': ['冲鸭！很快就好！', '正在变魔术...', '我们快搞定啦！'],
        'The Glitch': ['Working... *bzzzt*', 'Process.zip.rar', 'Woooooo!'],
        'The Analyst': ['向量计算中。', '神经网络激活。', '优化输出序列。']
      },
      success: {
        'The Critic': ['勉强及格。', '也就那样。', '终于完事了。', '下次给点有难度的。'],
        'The Sage': ['智慧之光。', '功德圆满。', '真相大白。'],
        'The Cheerleader': ['太棒了！击个掌！', '为你自豪！', 'Wow! 看看这结果！'],
        'The Glitch': ['Done.exe', 'SUCCESS_MAX', 'Bling!'],
        'The Analyst': ['任务执行完毕。', '准确率 99.9%。', '输出已送达。']
      }
    };
    let pool = library[event]?.[this.personality] || ['你好呀'];
    return pool[Math.floor(Math.random() * pool.length)];
  }

  init() {
    let textContainer = document.querySelector('header .min-w-0');
    if (!textContainer) return;
    let headerFlex = textContainer.parentNode;
    headerFlex.classList.add('items-center');
    
    let widget = document.getElementById('lumina-buddy');
    if (!widget) {
      widget = document.createElement('div');
      widget.id = 'lumina-buddy';
      widget.className = 'inline-flex items-center cursor-pointer relative group shrink-0 ml-4';
      widget.style.zIndex = '100';
      widget.title = `Lv.${this.level} ${this.personality} (${this.speciesKey})`;
      widget.innerHTML = `
        <div id="buddy-speech" class="hidden absolute bg-zinc-900 dark:bg-zinc-100 px-3 py-1.5 rounded-xl shadow-2xl whitespace-nowrap z-[200] buddy-speech-right">
          <span class="text-white dark:text-zinc-900 text-[10px] font-black leading-none block">你好呀！</span>
        </div>
        <pre id="buddy-canvas" class="font-mono text-[10px] font-black leading-none text-indigo-700 dark:text-indigo-300 select-none hover:scale-110 transition-all origin-center touch-manipulation" style="max-height: 52px;"></pre>
      `;
      headerFlex.appendChild(widget);
      headerFlex.style.overflow = 'visible';
      
      const handleInteraction = (e) => {
        if (e) { e.preventDefault(); e.stopPropagation(); }
        this.say(this.getDialogue(this.state === 'idle' ? 'idle' : this.state));
      };
      widget.addEventListener('click', handleInteraction);
      widget.addEventListener('touchstart', handleInteraction, { passive: false });
    }

    this.container = widget;
    this.canvas = document.getElementById('buddy-canvas');
    this.speech = document.getElementById('buddy-speech');
    
    this.startAnimation();
    this.scheduleBlink();
  }

  startAnimation() {
    if (this.timer) clearInterval(this.timer);
    this.timer = setInterval(() => this.render(), 400);
  }

  scheduleBlink() {
    if (this.blinkTimer) clearTimeout(this.blinkTimer);
    const delay = 2000 + Math.random() * 5000;
    this.blinkTimer = setTimeout(() => {
      this.isBlinking = true;
      setTimeout(() => { this.isBlinking = false; this.scheduleBlink(); }, 200);
    }, delay);
  }

  setState(state) {
    if (this.state === state) return;
    this.state = state;
    this.frameIndex = 0;
    this.say(this.getDialogue(state === 'idle' ? 'idle' : state), state === 'working' ? 5000 : 3000);
    if (state === 'success') this.addXP(25);
  }

  say(text, duration = 2500) {
    if (!this.speech) return;
    let span = this.speech.querySelector('span');
    if (span) span.textContent = text;
    this.speech.classList.remove('hidden');
    if (this.speechTimer) clearTimeout(this.speechTimer);
    this.speechTimer = setTimeout(() => this.speech.classList.add('hidden'), duration);
  }

  render() {
    const species = BUDDY_SPECIES[this.speciesKey] || BUDDY_SPECIES.cat;
    let frame = this.isBlinking && species.blink ? species.blink : species.frames[this.frameIndex % species.frames.length];
    this.frameIndex++;
    
    // Replace eyes
    let eyeStyle = this.state === 'working' ? 'round' : (this.state === 'success' ? 'cool' : (this.state === 'error' ? 'cross' : 'normal'));
    let eyeChar = BUDDY_EYES[eyeStyle];
    
    // Add Vibe Accents
    if (this.vibe === 'code') eyeChar = `[${eyeChar}]`; 
    if (this.vibe === 'web') eyeChar = `(${eyeChar})`;  
    if (this.vibe === 'chill') eyeChar = `z${eyeChar}z`;
    
    let content = frame.replace(/E/g, eyeChar);
    
    // Add stars for high levels (without trimming to preserve ASCII alignment)
    if (this.level >= 5) {
      content = content.split('\n').map((line, i) => i === 2 ? `★${line}★` : `  ${line}  `).join('\n');
    }
    this.canvas.textContent = content;

    // Rarity Classes (Update colors only)
    const rarities = [
      { name: 'Common', color: 'text-zinc-500/80', darkColor: 'dark:text-zinc-400/80' },
      { name: 'Rare', color: 'text-emerald-500', darkColor: 'dark:text-emerald-400' },
      { name: 'Legendary', color: 'text-amber-500', darkColor: 'dark:text-amber-400' }
    ];
    
    let h = 0;
    for (let i = 0; i < this.speciesKey.length; i++) h += this.speciesKey.charCodeAt(i);
    let rVal = Math.abs(this.userHash + h) % 100;
    let rIdx = rVal > 95 ? 2 : (rVal > 70 ? 1 : 0);
    
    // Clean old color classes and add new ones
    rarities.forEach(r => this.canvas.classList.remove(r.color, r.darkColor));
    this.canvas.classList.add(rarities[rIdx].color, rarities[rIdx].darkColor);
  }
}

window.luminaBuddy = null;
function initLuminaBuddy() {
  let keys = Object.keys(BUDDY_SPECIES);
  let user = document.getElementById('greeting')?.textContent || 'Lumina';
  let hash = 0;
  for (let i = 0; i < user.length; i++) hash = ((hash << 5) - hash) + user.charCodeAt(i), hash |= 0;
  window.luminaBuddy = new LuminaBuddy(keys[Math.abs(hash) % keys.length], hash);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initLuminaBuddy);
else initLuminaBuddy();
