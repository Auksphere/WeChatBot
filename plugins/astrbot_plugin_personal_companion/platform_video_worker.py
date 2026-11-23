"""Isolated anonymous platform video downloader. JSON stdin/stdout, no chat API."""
import json
import math
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit,urlunsplit,parse_qs,urlencode

MAX_BYTES=100*1024*1024
MAX_DURATION=4*3600
SHARE_HOSTS={'b23.tv','xhslink.com','xhslink.cn'}
PAGE_HOSTS={'bilibili.com','www.bilibili.com','m.bilibili.com','xiaohongshu.com','www.xiaohongshu.com'}
NETWORK_ROOTS=('bilibili.com','b23.tv','bilivideo.com','bilivideo.cn','hdslb.com','xiaohongshu.com','xhslink.com','xhslink.cn','xhscdn.com')

class PlatformVideoError(ValueError):pass


def check_url(url,*,share=False):
    if (not isinstance(url,str) or len(url)>16384 or '\\' in url or any(ord(c)<=32 for c in url)):
        raise PlatformVideoError('invalid_url')
    try:
        parts=urlsplit(url);host=(parts.hostname or '').lower()
        valid=host in SHARE_HOSTS|PAGE_HOSTS if share else any(host==root or host.endswith('.'+root) for root in NETWORK_ROOTS)
        if (parts.scheme not in {'http','https'} or not valid or parts.username is not None
                or parts.password is not None or parts.port not in {None,80,443}):raise ValueError()
    except ValueError:raise PlatformVideoError('invalid_url') from None
    return parts


def canonical_url(url):
    parts=check_url(url,share=True);host=parts.hostname.lower()
    if host in SHARE_HOSTS:return None
    if host.endswith('bilibili.com'):
        match=re.fullmatch(r'/(?:[a-zA-Z]{2}(?:-[a-zA-Z]{2,4})?/)?video/(BV[A-Za-z0-9]{10}|av[0-9]+)/*',parts.path)
        if not match:raise PlatformVideoError('unsupported_page')
        page=parse_qs(parts.query).get('p',['1'])[0]
        if not page.isdigit() or not 1<=int(page)<=1000:raise PlatformVideoError('unsupported_page')
        return 'https://www.bilibili.com/video/'+match[1]+'/?'+urlencode({'p':str(int(page))})
    match=re.fullmatch(r'/(?:explore|discovery/item)/([A-Fa-f0-9]{24})/*',parts.path)
    if not match:raise PlatformVideoError('unsupported_page')
    return urlunsplit(('https','www.xiaohongshu.com','/explore/'+match[1],parts.query,''))


class Quiet:
    def debug(self,*a,**k):pass
    warning=debug
    error=debug


def make_downloader(params):
    from yt_dlp import YoutubeDL
    from yt_dlp.networking._urllib import UrllibRH,RedirectHandler
    from yt_dlp.extractor.bilibili import BiliBiliIE
    from yt_dlp.extractor.xiaohongshu import XiaoHongShuIE
    class CheckedRedirect(RedirectHandler):
        handler_order=399
        def redirect_request(self,req,fp,code,msg,headers,newurl):
            check_url(newurl)
            return super().redirect_request(req,fp,code,msg,headers,newurl)
    class PlatformUrllibRH(UrllibRH):
        def _create_instance(self,*args,**kwargs):
            opener=super()._create_instance(*args,**kwargs);opener.add_handler(CheckedRedirect());return opener
        def _send(self,request):
            check_url(request.url)
            return super()._send(request)
    class PlatformYDL(YoutubeDL):
        def build_request_director(self,handlers,preferences=None):
            return super().build_request_director([PlatformUrllibRH],preferences=None)
    ydl=PlatformYDL(params,auto_init=False)
    ydl.add_info_extractor(BiliBiliIE());ydl.add_info_extractor(XiaoHongShuIE())
    return ydl


def classify_error(exc):
    if isinstance(exc,PlatformVideoError):return str(exc)
    if isinstance(exc,ModuleNotFoundError):return 'configuration_error'
    text=str(exc).lower()
    if any(x in text for x in ['login','logged in','登录','cookie','401','412','验证码','risk control','403','451','access denied']):return 'access_restricted'
    if any(x in text for x in ['404','not found','unavailable','不存在','失效']):return 'not_available'
    if any(x in text for x in ['no video','no formats','requested format','video formats']):return 'no_video'
    if 'timed out' in text or 'timeout' in text:return 'timeout'
    return 'download_failed'


def download(url,directory,ffmpeg):
    check_url(url,share=True)
    directory=Path(directory).resolve();ffmpeg=Path(ffmpeg).resolve()
    if not directory.is_dir() or not ffmpeg.is_file():raise PlatformVideoError('configuration_error')
    def progress(status):
        if sum(p.stat().st_size for p in directory.iterdir() if p.is_file())>MAX_BYTES:
            raise PlatformVideoError('too_large')
    params={'quiet':True,'no_warnings':True,'logger':Quiet(),'cachedir':False,
        'noplaylist':True,'skip_download':False,'socket_timeout':12,'retries':0,'extractor_retries':0,'fragment_retries':0,
        'continuedl':False,'nopart':False,'concurrent_fragment_downloads':1,'hls_prefer_native':True,
        'outtmpl':str(directory/'media.%(ext)s'),'ffmpeg_location':str(ffmpeg),'merge_output_format':'mp4',
        'format':'best[height<=480][vcodec!=none][acodec!=none]/bestvideo[height<=480]+bestaudio/best[height<=720]/bestvideo[height<=720]+bestaudio/best/bestvideo+bestaudio',
        'max_filesize':MAX_BYTES,'progress_hooks':[progress], 'overwrites':True,
        'cookiesfrombrowser':None,'cookiefile':None,'usenetrc':False,'enable_file_urls':False,
        'writeinfojson':False,'writesubtitles':False,'writeautomaticsub':False,'writethumbnail':False}
    with make_downloader(params) as ydl:
        canonical=canonical_url(url)
        if canonical is None:
            with ydl.urlopen(url) as response:canonical=canonical_url(response.url)
            if canonical is None:raise PlatformVideoError('unsupported_page')
        platform='bilibili' if urlsplit(canonical).hostname.endswith('bilibili.com') else 'xiaohongshu'
        info=ydl.extract_info(canonical,download=False,ie_key='BiliBili' if platform=='bilibili' else 'XiaoHongShu')
        if not isinstance(info,dict) or info.get('_type') in {'playlist','multi_video'}:raise PlatformVideoError('playlist_not_supported')
        if info.get('is_live') or info.get('live_status') in {'is_live','is_upcoming'}:raise PlatformVideoError('live_not_supported')
        duration=info.get('duration')
        if not isinstance(duration,(int,float)) or not math.isfinite(duration) or not 0<duration<=MAX_DURATION:
            raise PlatformVideoError('invalid_duration')
        if info.get('has_drm'):raise PlatformVideoError('access_restricted')
        tracks=info.get('requested_formats') or [info]
        for track in tracks:
            if not isinstance(track.get('url'),str):raise PlatformVideoError('no_video')
            check_url(track['url'])
            if track.get('protocol') not in {'http','https',None}:raise PlatformVideoError('unsupported_protocol')
        size=sum((f.get('filesize') or f.get('filesize_approx') or 0) for f in tracks)
        if size>MAX_BYTES:raise PlatformVideoError('too_large')
        ydl.process_info(info)
        allowed={'.mp4','.mkv','.webm','.flv','.mov'}
        candidates=[p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in allowed and not re.search(r'\.f[0-9]+\.',p.name)]
        if len(candidates)!=1:raise PlatformVideoError('download_failed')
        result=candidates[0].resolve()
        if not result.is_relative_to(directory) or not 0<result.stat().st_size<=MAX_BYTES:raise PlatformVideoError('too_large')
        return {'ok':True,'path':str(result),'platform':platform,'title':str(info.get('title') or '')[:300],
            'uploader':str(info.get('uploader') or '')[:150],'duration':duration,'source_url':canonical,'byte_length':result.stat().st_size}


def download_with_retry(url,directory,ffmpeg):
    directory=Path(directory).resolve()
    for attempt in (1,2):
        try:
            result=download(url,directory,ffmpeg);result['attempts']=attempt
            return result
        except Exception as exc:
            code=classify_error(exc)
            if attempt==2 or code not in {'no_video','download_failed','timeout'}:
                error=PlatformVideoError(code);error.attempts=attempt
                raise error from None
            # Only files created under this invocation's private temp directory.
            for item in directory.iterdir():
                if (item.name.startswith('media.') and item.is_file() and not item.is_symlink()
                        and item.resolve().is_relative_to(directory)):
                    item.unlink()
            time.sleep(1)
    raise PlatformVideoError('download_failed')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        value=json.loads(sys.stdin.buffer.read(24000).decode('utf-8'))
        result=download_with_retry(value['url'],value['directory'],value['ffmpeg'])
    except BaseException as exc:
        result={'ok':False,'error':classify_error(exc),'attempts':getattr(exc,'attempts',1)}
    print(json.dumps(result,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
