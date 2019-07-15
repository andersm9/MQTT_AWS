import os
import configparser
from datetime import datetime
from datetime import timedelta
from subprocess import Popen
import sys
import time
import requests
import boto3
import json

import paho.mqtt.client as mqtt

session = requests.Session()

def meraki_snapshots(session, api_key, net_id, time=None, filters=None):
    # Get devices of network and filter for MV cameras
    headers = {
        'X-Cisco-Meraki-API-Key': api_key,
        # 'Content-Type': 'application/json'  # issue where this is only needed if timestamp specified
    }
    response = session.get(f'https://api.meraki.com/api/v0/networks/{net_id}/devices',headers=headers)
    devices = response.json()
    cameras = [device for device in devices if device['model'][:2] == 'MV']

    # Assemble return data
    snapshots = []
    for camera in cameras:
        # Remove any cameras not matching filtered names
        name = camera['name'] if 'name' in camera else camera['mac']
        tags = camera['tags'] if 'tags' in camera else ''
        tags = tags.split()
        if filters and name not in filters and not set(filters).intersection(tags):
            continue

        # Get video link
        if time:
            response = session.get(
                f'https://api.meraki.com/api/v0/networks/{net_id}/cameras/{camera["serial"]}/videoLink?timestamp={time}',
                headers=headers)
        else:
            response = session.get(
                f'https://api.meraki.com/api/v0/networks/{net_id}/cameras/{camera["serial"]}/videoLink',
                headers=headers)
        video_link = response.json()['url']

        # Get snapshot link
        if time:

            headers['Content-Type'] = 'application/json'
            response = session.post(
                f'https://api.meraki.com/api/v0/networks/{net_id}/cameras/{camera["serial"]}/snapshot',
                headers=headers,
                data=json.dumps({'timestamp': time}))
        else:
            response = session.post(
                f'https://api.meraki.com/api/v0/networks/{net_id}/cameras/{camera["serial"]}/snapshot',
                headers=headers)

        # Possibly no snapshot if camera offline, photo not retrievable, etc.
        if response.ok:
            snapshots.append((name, response.json()['url'], video_link))

    return snapshots


# Store credentials in a separate file
def gather_credentials():
    cp = configparser.SafeConfigParser(allow_no_value=True)
    try:
        cp.read('credentials.ini')
        cam_key = cp.get('meraki', 'key2')
        net_id = cp.get('meraki', 'network')
        mv_serial = cp.get('sense', 'serial')
    except:
        print('Missing credentials or input file!')
        sys.exit(2)
    return cam_key, net_id,mv_serial

def snap(image):
    #the image is not available in the Meraki API instantly:
    #time.sleep(5)
    print("in snap")
    session = boto3.Session(profile_name='default')
    rek = session.client('rekognition')
    resp = requests.get(image)
    print(resp)
    rekresp = {}
    resp_txt = str(resp)
    imgbytes = resp.content
    try:
        rekresp = rek.detect_faces(Image={'Bytes': imgbytes}, Attributes=['ALL'])
    except:
        pass
    return(rekresp, resp_txt)

#get labels (e.g House, car etc)
def detect_labels(image, max_labels=10, min_confidence=90):
    rekognition = boto3.client("rekognition")
    resp = requests.get(image)
    imgbytes = resp.content
    label_response = rekognition.detect_labels(
        Image={'Bytes': imgbytes},
        MaxLabels=max_labels,
        MinConfidence=min_confidence,
    )
    #print(str(label_response))
    return label_response['Labels']


# The callback for when the client receives a CONNACK response from the server
def on_connect(client, userdata, flags, rc):
    print(f'Connected with result code {rc}')
    serial = userdata['mv_serial']
    # Subscribing in on_connect() means that if we lose the connection and
    # reconnect then subscriptions will be renewed.
    client.subscribe(f'/merakimv/{serial}/0')
    #client.subscribe(f'/merakimv/{serial}/light')

# The callback for when a PUBLISH message is received from the server
def on_message(client, userdata, msg):
    datum = str(msg.payload)
    #print(f'{msg.topic} {datum}')
    analyze(datum, userdata)


# Generic parse function
def parse(text, beg_tag, end_tag, beg_pos=0, end_pos=-1):
    if text.find(beg_tag, beg_pos, end_pos) == -1:
        return ('', -1)
    else:
        initial = text.find(beg_tag, beg_pos, end_pos) + len(beg_tag)
    if text.find(end_tag, initial) == -1:
        return ('', -1)
    else:
        final = text.find(end_tag, initial)
    return (text[initial:final], final+len(end_tag))

LAST_COUNT = 0
LAST_TIME = 0
def analyze(datum, userdata):
    global LAST_COUNT, LAST_TIME
    datum = str(datum)
    time = int(parse(datum, '{"ts":', ',')[0])
    count = int(parse(datum, '{"person":', '}}')[0])

    # Analyze only if people are detected
    if True:
        #Send snapshot once per second
        if time > LAST_TIME + 1000:
            print(" time has elapsed, take picture")
            LAST_TIME = time
            LAST_COUNT = count
            snapshots = meraki_snapshots(session, api_key, net_id, None, None)
            resp_txt = "404"
            while ("404" in resp_txt) == True:
                rekresp, resp_txt = snap(snapshots[0][1])
                print(resp_txt)
            for faceDetail in rekresp['FaceDetails']:
                print('The detected face is between ' + str(faceDetail['AgeRange']['Low']) + ' and ' + str(faceDetail['AgeRange']['High']) + ' years old')
                Age = (((faceDetail['AgeRange']['Low'])+(faceDetail['AgeRange']['High']))/2)
                EmotionalState = max(faceDetail['Emotions'], key=lambda x:x['Confidence'])
                Emotion = EmotionalState['Type']
                gender = (faceDetail['Gender']['Value'])
                print(gender)
                print(Emotion)
                print(Age)
                Publish = {Age:EmotionalState['Type']}
                client.publish("Age",Age)
                client.publish("EmotionalState",Emotion)
                client.publish("Gender",gender)
            labels=[]
            n=0
            print("going into Objects_Detected")
            Objects_Detected = detect_labels(snapshots[0][1])
            print("got Objects_Detected")
            for label in Objects_Detected:
                entry = str("{Name} - {Confidence}%".format(**label))
                labels.append(entry)
                #print(entry)
                label = ("Label" + str(n))
                print(label + " " + entry)
                client.publish(label,entry)
                #print(n)
                #print("label"+str(n))
                n = n+1
            print("end of objects detected")

# Main function
if __name__ == '__main__':
    # Get credentials
    (api_key, net_id,mv_serial) = gather_credentials()
    user_data = {
        'api_key': api_key,
        'net_id': net_id,
        'mv_serial': mv_serial
    }
    session = requests.Session()
    # Start MQTT client
    client = mqtt.Client()
    client.user_data_set(user_data)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect('localhost', 1883, 300)
    client.loop_forever()
