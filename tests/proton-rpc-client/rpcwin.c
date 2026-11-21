#include <windows.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
static HANDLE h = INVALID_HANDLE_VALUE;
static HWND label;
static char status[512] = "Connecting to discord-ipc-0...";
static void setstatus(const char *s){ lstrcpynA(status, s, sizeof status); if(label) SetWindowTextA(label, status); }
static int sendf(int op,const char*j){DWORD n,l=strlen(j);int hd[2]={op,(int)l};
 return WriteFile(h,hd,8,&n,0)&&WriteFile(h,j,l,&n,0);}
static int recvp(char*b,int cap){int hd[2];DWORD n;if(!ReadFile(h,hd,8,&n,0)||n!=8)return 0;
 DWORD l=(DWORD)hd[1]<(DWORD)cap-1?(DWORD)hd[1]:(DWORD)cap-1;if(!ReadFile(h,b,l,&n,0))return 0;b[n]=0;return 1;}
static DWORD WINAPI rpc(LPVOID p){
 char j[1024],b[4096];
 h=CreateFileA("\\\\.\\pipe\\discord-ipc-0",GENERIC_READ|GENERIC_WRITE,0,0,OPEN_EXISTING,0,0);
 if(h==INVALID_HANDLE_VALUE){snprintf(j,sizeof j,"No Discord pipe (error %lu).\nThe bridge is not running in this prefix.",GetLastError());setstatus(j);return 1;}
 snprintf(j,sizeof j,"{\"v\":1,\"client_id\":\"%s\"}",(const char*)p);sendf(0,j);
 if(!recvp(b,sizeof b)||!strstr(b,"READY")){setstatus("Handshake failed.");return 1;}
 snprintf(j,sizeof j,"{\"cmd\":\"SET_ACTIVITY\",\"nonce\":\"1\",\"args\":{\"pid\":%lu,\"activity\":{\"details\":\"rpc-bridge test from Proton\",\"state\":\"Wine prefix\",\"timestamps\":{\"start\":%lld}}}}",GetCurrentProcessId(),(long long)time(NULL));
 sendf(1,j);
 if(recvp(b,sizeof b)&&strstr(b,"SET_ACTIVITY"))setstatus("Status sent to Discord through rpc-bridge.\n\nClose this window to stop.");
 else setstatus("SET_ACTIVITY got no answer.");
 while(recvp(b,sizeof b)){}  /* hold the connection open */
 setstatus("Discord connection closed.");return 0;}
static LRESULT CALLBACK wp(HWND w,UINT m,WPARAM a,LPARAM l){
 if(m==WM_COMMAND&&LOWORD(a)==1){DestroyWindow(w);return 0;}
 if(m==WM_DESTROY){PostQuitMessage(0);return 0;}
 return DefWindowProcA(w,m,a,l);}
int WINAPI WinMain(HINSTANCE hi,HINSTANCE pi,LPSTR cmd,int show){
 static char id[64]="1167674267748540516"; if(cmd&&*cmd) lstrcpynA(id,cmd,sizeof id);
 WNDCLASSA wc={0};wc.lpfnWndProc=wp;wc.hInstance=hi;wc.lpszClassName="rpcwin";wc.hbrBackground=(HBRUSH)(COLOR_WINDOW+1);wc.hCursor=LoadCursor(0,IDC_ARROW);
 RegisterClassA(&wc);
 HWND w=CreateWindowA("rpcwin","rpc-bridge test",WS_OVERLAPPEDWINDOW|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,640,360,0,0,hi,0);
 HFONT f=CreateFontA(28,0,0,0,FW_NORMAL,0,0,0,DEFAULT_CHARSET,0,0,0,0,"Segoe UI");
 label=CreateWindowA("STATIC",status,WS_CHILD|WS_VISIBLE|SS_CENTER,20,30,600,180,w,0,hi,0);
 HWND btn=CreateWindowA("BUTTON","Exit",WS_CHILD|WS_VISIBLE|BS_DEFPUSHBUTTON,240,230,160,60,w,(HMENU)1,hi,0);
 SendMessageA(label,WM_SETFONT,(WPARAM)f,1);SendMessageA(btn,WM_SETFONT,(WPARAM)f,1);
 CreateThread(0,0,rpc,id,0,0);
 MSG msg;while(GetMessageA(&msg,0,0,0)>0){if(!IsDialogMessageA(w,&msg)){TranslateMessage(&msg);DispatchMessageA(&msg);}}
 if(h!=INVALID_HANDLE_VALUE)CloseHandle(h);
 ExitProcess(0);}
