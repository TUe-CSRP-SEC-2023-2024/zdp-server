from requests_html import HTMLSession
from flask import Flask, request, jsonify, render_template
import hashlib
from collections import defaultdict
import socket
import ssl
import tldextract
from utils import domains

from utils.customlogger import CustomLogger
import time
import os
import signal
from parsing import Parsing
from utils.reverseimagesearch import ReverseImageSearch
from engines.google import GoogleReverseImageSearchEngine
import sqlite3
from urllib.parse import urlparse
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
import utils.classifiers as cl
import joblib
from utils.sessions import Sessions

# To avoid RuntimeError('This event loop is already running') when there are many of requests
import nest_asyncio

# Option for saving the taken screenshots
SAVE_SCREENSHOT_FILES = False
# Whether to use the Clearbit logo API (see https://clearbit.com/logo)
USE_CLEARBIT_LOGO_API = True

# Where to store temporary session files, such as screenshots
SESSION_FILE_STORAGE_PATH = "files/"
# Database path for the operational output (?)
DB_PATH_OUTPUT = "db/output_operational.db"
# Database path for the sessions
DB_PATH_SESSIONS = "db/sessions.db"

# Page loading timeout for web driver
WEB_DRIVER_TIMEOUT = 5


# The storage interface for the sessions
sessions = Sessions(DB_PATH_SESSIONS, False)

# The main logger for the whole program, singleton
main_logger = CustomLogger().main_logger

# The HTTP + HTML session to use for reverse image search
html_session = HTMLSession()
html_session.browser # TODO why is this here

# The logo classifier, deserialized from file
logo_classifier = joblib.load('saved-classifiers/gridsearch_clf_rt_recall.joblib')

# Initiate Flask app
app = Flask(__name__)
app.config["DEBUG"] = False


@app.route('/')
def home():
    return render_template('index.html')

@app.route('/stop')
def shutdown():
    shutdown_server()
    return 'Server shutting down...'

def shutdown_server():
    os._exit(0)

@app.route('/api/v1/url', methods=['POST'])
def check_url():
    startTime = time.time()
    json = request.get_json()
    
    # main_logger.debug("Received JSON: " + str(json))
    # main_logger.warn("Received JSON: " + str(json))
    # main_logger.warn("Received JSON: " + str(json["URL"]))
        
    url = json["URL"]
    uuid = json["uuid"]
    main_logger.info(f'''

##########################################################
##### Request received for URL:\t{url}
##########################################################
''')

    # extra json field for evaluation purposes
    # the hash computed in the DB is the this one
    if "phish_url" in json:
        url = json["phishURL"]
        main_logger.info(f"Real URL changed to phishURL: {url}\n")
    else:
        main_logger.info("Not a phish URL, real URL")

    url_domain = domains.get_hostname(url)
    url_registered_domain = domains.get_registered_domain(url_domain)
    url_hash = hashlib.sha1(url.encode('utf-8')).hexdigest() # TODO: switch to better hash, cause SHA-1 broken?

    session_file_path = os.path.join(SESSION_FILE_STORAGE_PATH, url_hash)

    # Check if URL is in cache or still processing
    cache_result = sessions.get_state(uuid, url)
    # main_logger.info(f"Request in cache: {cache_result}")
    if cache_result != 'new':
        # Request is already in cache, use result from that (possibly waiting until it is finished)
        if cache_result[0] == 'processing':
            time.sleep(4) # TODO: oh god
        
        stopTime = time.time()
        main_logger.warn(f"Time elapsed for {url} is {stopTime - startTime}s, found in cache with result {cache_result[0]}")
        
        result = [{'url': url, 'status': cache_result[0], 'sha1': url_hash}]
        return jsonify(result)
    
    # Update the current state in the session storage
    sessions.store_state(uuid, url, 'processing', 'textsearch')

    # Take screenshot of requested page
    parsing = Parsing(SAVE_SCREENSHOT_FILES, json=json, store=session_file_path)

    db_conn_output = sqlite3.connect(DB_PATH_OUTPUT)

    # Perform text search of the screenshot
    try: # timed
        comp_start_time = time.time()

        # Initiate text-only reverse image search instance
        search = ReverseImageSearch(storage=DB_PATH_OUTPUT,
                                    search_engine=list(GoogleReverseImageSearchEngine().identifiers())[0],
                                    folder=SESSION_FILE_STORAGE_PATH,
                                    upload=False,
                                    mode="text",
                                    htmlsession=html_session,
                                    clf=logo_classifier)
        
        search.handle_folder(session_file_path, url_hash)
        
        # Get result from the above search
        url_list_text = db_conn_output.execute("SELECT DISTINCT result FROM search_result_text WHERE filepath = ?", [url_hash]).fetchall()
    finally:
        comp_end_time = time.time()
        comp_time_diff = comp_end_time - comp_start_time
        main_logger.warn(f"Time elapsed for text find for {url_hash} is {comp_time_diff}s")

    # Handle results of search from above
    res = check_search_results(uuid, url, url_hash, url_registered_domain, url_list_text, startTime)
    if res != None:
        return res
    
    # No match through text, move on to image search
    sessions.store_state(uuid, url, 'processing', 'imagesearch')

    search = ReverseImageSearch(storage=DB_PATH_OUTPUT, 
                                search_engine=list(GoogleReverseImageSearchEngine().identifiers())[0], 
                                folder=SESSION_FILE_STORAGE_PATH, 
                                upload=True, mode="image", 
                                htmlsession=html_session, 
                                clf=logo_classifier, 
                                clearbit=USE_CLEARBIT_LOGO_API, 
                                tld=url_registered_domain)
    search.handle_folder(session_file_path, url_hash)
    
    url_list_img = db_conn_output.execute("SELECT DISTINCT result FROM search_result_image WHERE filepath = ?", [url_hash]).fetchall()

    res = check_search_results(uuid, url, url_hash, url_registered_domain, url_list_img, startTime)
    if res != None:
        return res

    # No match through images, go on to image comparison per URL

    ######
    compareST = time.time()
    ######

    sessions.store_state(uuid, url, 'processing', 'imagecompare')
    
    out_dir = os.path.join('compare_screens', url_hash)
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
    options = Options()
    options.add_argument( "--headless" )

    url_list_img_cmp = url_list_text + url_list_img

    # Initialize web driver
    driver = webdriver.Chrome(options=options)
    image_width, image_height = parsing.get_size()
    driver.set_window_size(image_width, image_height)
    driver.set_page_load_timeout(WEB_DRIVER_TIMEOUT)

    for index, resulturl in enumerate(url_list_img_cmp):
        if (not isinstance(resulturl[0], str)):
            continue
        urllower = resulturl[0].lower()
        
        # TODO whyyyyyyy
        if (("www.mijnwoordenboek.nl/puzzelwoordenboek/Dot/1" in resulturl[0]) or 
                ("amsterdamvertical" in resulturl[0]) or ("dotgroningen" in urllower) or 
                ("britannica" in resulturl[0]) or 
                ("en.wikipedia.org/wiki/Language" in resulturl[0]) or 
                (resulturl[0] == '') or 
                (("horizontal" in urllower) and 
                    not ("horizontal" in domains.get_registered_domain(resulturl[0])) 
                    or (("vertical" in urllower) and not ("horizontal" in domains.get_registered_domain(resulturl[0]))))):
            continue
        
        # Take screenshot of URL and save it
        try:
            driver.get(resulturl[0])
        except:
            continue
        driver.save_screenshot(out_dir + "/" + str(index) + '.png')

        # image compare
        path_a = os.path.join(session_file_path, "screen.png")
        path_b = out_dir + "/" + str(index) + ".png"
        emd = None
        dct = None
        s_sim = None
        p_sim = None
        orb = None
        try:
            emd = cl.earth_movers_distance(path_a, path_b)
        except Exception as err:
            main_logger.error(err)
        try:
            dct = cl.dct(path_a, path_b)
        except Exception as err:
            main_logger.error(err)
        try:
            s_sim = cl.structural_sim(path_a, path_b)
        except Exception as err:
            main_logger.error(err)
        try:
            p_sim = cl.pixel_sim(path_a, path_b)
        except Exception as err:
            main_logger.error(err)
        try:
            orb = cl.orb_sim(path_a, path_b)
        except Exception as err:
            main_logger.error(err)
        main_logger.info(f"Compared url '{resulturl[0]}'")
        main_logger.info(f"Finished comparing:  emd = '{emd}', dct = '{dct}', pixel_sim = '{p_sim}', structural_sim = '{s_sim}', orb = '{orb}'")
        
        # return phishing if very similar
        if ((emd < 0.001) and (s_sim > 0.70)) or ((emd < 0.002) and (s_sim > 0.80)):
            driver.quit()
            stopTime = time.time()
            main_logger.warn(f"Time elapsed for {url} is {stopTime - startTime}s with result phishing")
            sessions.store_state(uuid, url, 'phishing', '')
            
            result = [{'url': url, 'status': "phishing", 'sha1': url_hash}]
            return jsonify(result)
        #otherwise go to next
    
    ######
    compareSPT = time.time()
    main_logger.warn(f"Time elapsed for imgCompare find for {url_hash} is {compareSPT - compareST}s")
    ######

    driver.quit()

    stopTime = time.time()
    
    # if the inconclusive stems from google blocking:
    #   e.g. blocked == True
    #   result: inconclusive_blocked
    
    main_logger.warn(f"Time elapsed for {url} is {stopTime - startTime}s with result inconclusive")
    result = [{'url': url, 'status': "inconclusive", 'sha1': url_hash}]
    sessions.store_state(uuid, url, 'inconclusive', '')
    return jsonify(result)

@app.route('/api/v1/url/state', methods=['POST'])
def get_url_state():
    json = request.get_json()
    url = json["URL"]
    uuid = json["uuid"]
    
    currStatus = sessions.get_state(uuid, url)
    
    result = [{'status': currStatus[0], 'state': currStatus[1]}]
    return jsonify(result)


# TODO move to other file, same with some funkys above
def check_search_results(uuid, url, url_hash, url_registered_domain, found_urls, startTime):
    sanTextST = time.time()

    domain_list_tld_extract = set()
    # Get SAN names and append
    for urls in found_urls:
        domain = domains.get_hostname(urls[0]) # TODO remove index requirement
        try:
            san_names = [domain] + domains.get_san_names(domain)
        except:
            main_logger.error(f'Error in SAN for {domain}')
            continue
        
        for hostname in san_names:
            registered_domain = domains.get_registered_domain(hostname)
            domain_list_tld_extract.append(registered_domain)

    ######
    sanTextSPT = time.time()
    main_logger.warn(f"Time elapsed for textSAN for {url_hash} is {sanTextSPT - sanTextST}s for {len(found_urls)} domains")
    ######
    
    if url_registered_domain in domain_list_tld_extract:
        print('Found in domain list')
        stopTime = time.time()
        main_logger.warn(f"Time elapsed for {url} is {stopTime - startTime}s with result not phishing")
        sessions.store_state(uuid, url, 'not phishing', '')
        
        result = [{'url': url, 'status': "not phishing", 'sha1': url_hash}]
        return jsonify(result)

    # no results yet
    return None

# Using this lib to avoid runtimerror with many requests
#__import__('IPython').embed()
nest_asyncio.apply()

# Handle CTRL+C for shutdown
def signal_handler(sig, frame):
    shutdown_server()
signal.signal(signal.SIGINT, signal_handler)

# Start Flask app, bind to all interfaces
app.run(host="0.0.0.0")
