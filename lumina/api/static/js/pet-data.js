/**
 * Lumina Buddy - Pet Species Data
 * 18 species from the classic Claude Buddy set.
 * Format: 5 lines x 12 chars (approx).
 * Frames: [Idle1, Idle2, Idle3], blink: BlinkFrame
 */

const BUDDY_EYES = {
  normal: '··',
  cool: '✦✦',
  cross: '××',
  round: '◉◉',
  at: '@@',
  degree: '°°'
};

const BUDDY_SPECIES = {
  duck: {
    name: "Duck",
    frames: [
      "            \n   <(E)__   \n    (   /   \n     ---    \n            ",
      "            \n   <(E)__   \n    (  _/   \n     ---    \n            ",
      "            \n   <(E)__   \n    (   /   \n     ---    \n            "
    ],
    blink: "            \n   <(--)__  \n    (   /   \n     ---    \n            "
  },
  cat: {
    name: "Cat",
    frames: [
      "  /\\_/\\     \n ( E )     \n  > ^ <     \n            \n            ",
      "  /\\_/\\     \n ( E )     \n  > ^ <     \n            \n            ",
      "  /\\_/\\     \n ( E )     \n  > ^ <     \n            \n            "
    ],
    blink: "  /\\_/\\     \n ( - - )    \n  > ^ <     \n            \n            "
  },
  owl: {
    name: "Owl",
    frames: [
      "  ,___,     \n  (E)     \n  /)  )     \n  \"--\"      \n            ",
      "  ,___,     \n  (E)     \n  /)  )     \n  \"--\"      \n            ",
      "  ,___,     \n  (E)     \n  /)  )     \n  \"--\"      \n            "
    ],
    blink: "  ,___,     \n  (- -)     \n  /)  )     \n  \"--\"      \n            "
  },
  rabbit: {
    name: "Rabbit",
    frames: [
      "  (\\ /)     \n  ( E )     \n  c(\")(\")   \n            \n            ",
      "  (\\ /)     \n  ( E )     \n  c(\")(\")   \n            \n            ",
      "  (\\ /)     \n  ( E )     \n  c(\")(\")   \n            \n            "
    ],
    blink: "  (\\ /)     \n  (- -)     \n  c(\")(\")   \n            \n            "
  },
  octopus: {
    name: "Octopus",
    frames: [
      "   _---_    \n  ( E )   \n   /\\ /\\    \n  /  |  \\   \n            ",
      "   _---_    \n  ( E )   \n   /| |\\    \n  /  |  \\   \n            ",
      "   _---_    \n  ( E )   \n   /\\ /\\    \n  /  |  \\   \n            "
    ],
    blink: "   _---_    \n  (- - )   \n   /\\ /\\    \n  /  |  \\   \n            "
  },
  dragon: {
    name: "Dragon",
    frames: [
      "  <>_      \n / E \\____ \n \\  /     \\\n  \"\"  \"\"\"\"  \n            ",
      "  <>_      \n / E \\____ \n \\  /     \\\n  \"\"  \"\"\"\"  \n            ",
      "  <>_      \n / E \\____ \n \\  /     \\\n  \"\"  \"\"\"\"  \n            "
    ],
    blink: "  <>_      \n / -- \\____\n \\  /     \\\n  \"\"  \"\"\"\"  \n            "
  },
  turtle: {
    name: "Turtle",
    frames: [
      "    ____    \n   /    \\   \n <( E )  ) \n  --  --    \n            ",
      "    ____    \n   /    \\   \n <( E )  ) \n  --  --    \n            ",
      "    ____    \n   /    \\   \n <( E )  ) \n  --  --    \n            "
    ],
    blink: "    ____    \n   /    \\   \n <(- -)  ) \n  --  --    \n            "
  },
  snail: {
    name: "Snail",
    frames: [
      "   _@_      \n  /   \\     \n ( E )___  \n  \"\"\"\"\"\"\"   \n            ",
      "   _@_      \n  /   \\     \n ( E )___  \n  \"\"\"\"\"\"\"   \n            ",
      "   _@_      \n  /   \\     \n ( E )___  \n  \"\"\"\"\"\"\"   \n            "
    ],
    blink: "   _@_      \n  /   \\     \n (- -)___  \n  \"\"\"\"\"\"\"   \n            "
  },
  penguin: {
    name: "Penguin",
    frames: [
      "   (E)     \n  /| |\\     \n  /_|_\\     \n   \" \"      \n            ",
      "   (E)     \n  /| |\\     \n  /_|_\\     \n   \" \"      \n            ",
      "   (E)     \n  /| |\\     \n  /_|_\\     \n   \" \"      \n            "
    ],
    blink: "   (- -)    \n  /| |\\     \n  /_|_\\     \n   \" \"      \n            "
  },
  ghost: {
    name: "Ghost",
    frames: [
      "   .-.      \n  ( E )     \n  |   |     \n  'u-u'     \n            ",
      "   .-.      \n  ( E )     \n  |   |     \n  'u-u'     \n            ",
      "   .-.      \n  ( E )     \n  |   |     \n  'u-u'     \n            "
    ],
    blink: "   .-.      \n  (- -)     \n  |   |     \n  'u-u'     \n            "
  },
  robot: {
    name: "Robot",
    frames: [
      "  [ E ]     \n  /| |\\     \n  /_|_\\     \n  \"   \"     \n            ",
      "  [ E ]     \n  /| |\\     \n  /_|_\\     \n  \"   \"     \n            ",
      "  [ E ]     \n  /| |\\     \n  /_|_\\     \n  \"   \"     \n            "
    ],
    blink: "  [- -]     \n  /| |\\     \n  /_|_\\     \n  \"   \"     \n            "
  },
  cactus: {
    name: "Cactus",
    frames: [
      "   _|_      \n  | E |     \n  |_|_|     \n    |       \n            ",
      "   _|_      \n  | E |     \n  |_|_|     \n    |       \n            ",
      "   _|_      \n  | E |     \n  |_|_|     \n    |       \n            "
    ],
    blink: "   _|_      \n  |- -|     \n  |_|_|     \n    |       \n            "
  },
  mushroom: {
    name: "Mushroom",
    frames: [
      "  .---.     \n (  E  )    \n  '---'     \n   | |      \n            ",
      "  .---.     \n (  E  )    \n  '---'     \n   | |      \n            ",
      "  .---.     \n (  E  )    \n  '---'     \n   | |      \n            "
    ],
    blink: "  .---.     \n ( - - )    \n  '---'     \n   | |      \n            "
  },
  blob: {
    name: "Blob",
    frames: [
      "  .---.     \n / E \\    \n(     )    \n '---'     \n            ",
      "  .---.     \n / E \\    \n(     )    \n '---'     \n            ",
      "  .---.     \n / E \\    \n(     )    \n '---'     \n            "
    ],
    blink: "  .---.     \n / - - \\    \n(     )    \n '---'     \n            "
  },
  chonk: {
    name: "Chonk",
    frames: [
      "  /\\---/\\   \n (  E  )  \n (      )  \n  '----'   \n            ",
      "  /\\---/\\   \n (  E  )  \n (      )  \n  '----'   \n            ",
      "  /\\---/\\   \n (  E  )  \n (      )  \n  '----'   \n            "
    ],
    blink: "  /\\---/\\   \n ( -   - )  \n (      )  \n  '----'   \n            "
  },
  capybara: {
    name: "Capybara",
    frames: [
      "   _---_    \n  ( E  )   \n  /     \\  \n  \"\"   \"\"   \n            ",
      "   _---_    \n  ( E  )   \n  /     \\  \n  \"\"   \"\"   \n            ",
      "   _---_    \n  ( E  )   \n  /     \\  \n  \"\"   \"\"   \n            "
    ],
    blink: "   _---_    \n  (-   -)   \n  /     \\  \n  \"\"   \"\"   \n            "
  },
  axolotl: {
    name: "Axolotl",
    frames: [
      "  - - -     \n (  E  )    \n  - - -     \n  \"   \"     \n            ",
      "  - - -     \n (  E  )    \n  - - -     \n  \"   \"     \n            ",
      "  - - -     \n (  E  )    \n  - - -     \n  \"   \"     \n            "
    ],
    blink: "  - - -     \n ( - - )    \n  - - -     \n  \"   \"     \n            "
  },
  goose: {
    name: "Goose",
    frames: [
      "    _       \n __/ E )    \n(   __/     \n \"\"\"\"       \n            ",
      "    _       \n __/ E )    \n(   __/     \n \"\"\"\"       \n            ",
      "    _       \n __/ E )    \n(   __/     \n \"\"\"\"       \n            "
    ],
    blink: "    _       \n __/ - )    \n(   __/     \n \"\"\"\"       \n            "
  }
};
