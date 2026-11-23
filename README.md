# Chess-bot
this is 100% vibecoded fyi
A tool that scans your screen for any chess games that analyses the board and gives the best moves live. I recommend to use a second monitor for the tool

Make sure you download stockfish 19 from https://github.com/official-stockfish/Stockfish/archive/refs/tags/sf_19.zip or https://stockfishchess.org/download/
(I didnt test if it works with any other version of stockfish)

Download the .py and .bat file, then run the .bat file. 
You will see 2 folders, build and dist. Open dist, then you need to drop 4 things into the dist folder: 
-stockfish.exe
-the "wiki" folder
-the "src" folder 
-the "scripts" folder

After that you can run ChessAssistant.exe. The rest should be pretty straightforward. 
Press "find board" to automatically scan your monitors for a chess board. Make sure the chess board isn't obstructed. 
Then, click "Test read" to test if the bot can read the board. 
To start the game, click "Start". The tool will automatically run stockfish and give you the best moves. 

As of 23 november of 2026, there are a lot of new features, main ones being: 
-A slider to adjust the elo stockfish thinks at, keep this about 150-200 elo above your chess.com elo, as you will get banned for playing like Magnus on a 2 day old account!!
Last game result button, dont win too much in a row as that will also ban you. 
-Mini mode, makes the window tiny and just shows the best move as a text. 
Compact UI, settings are moved to the actual settings menu. 
