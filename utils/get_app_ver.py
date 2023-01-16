from bs4 import BeautifulSoup
import requests
from os import environ

def get_app_ver_qooapp(appid) -> str:
    url = f'https://apps.qoo-app.com/en/app/{appid}'
    
    r = requests.get(url, headers={
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:108.0) Gecko/20100101 Firefox/108.0'
    })

    soup = BeautifulSoup(r.text, 'lxml')
    app_info_tree = soup.find(class_="app-info android")
    
    var_text = app_info_tree.find_all(class_="row")[1].var.text
    environ['APP_VER'] = var_text
    
    return var_text
