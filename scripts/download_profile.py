import urllib.request

headers = {
    'User-Agent': 'Mozilla/5.0'
}

req = urllib.request.Request("https://www.tennisexplorer.com/player/aboian-f7ddc/", headers=headers)
html = urllib.request.urlopen(req).read().decode('utf-8')

with open("aboian_profile.html", "w", encoding="utf-8") as f:
    f.write(html)
