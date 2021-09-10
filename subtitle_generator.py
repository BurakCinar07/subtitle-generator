import datetime
import os
import subprocess
import urllib.request

import psycopg2
import srt
from google.cloud import speech_v1
from google.cloud import storage
from google.cloud.speech_v1 import enums
from pydub.utils import mediainfo

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "credentials.json"
BUCKET_NAME = ""  # update this with your bucket name
FTP_BASE_URL = ""
LECTURE_ID = 0


def upload_blob(bucket_name, source_file_name, destination_blob_name):
    """Uploads a file to the bucket."""
    # bucket_name = "your-bucket-name"
    # source_file_name = "local/path/to/file"
    # destination_blob_name = "storage-object-name"

    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(destination_blob_name)

    blob.upload_from_filename(source_file_name, timeout=15000)

    print(
        "File {} uploaded to {}.".format(
            source_file_name, destination_blob_name
        )
    )


def download_video(link, video_name):
    try:
        urllib.request.urlretrieve(link, video_name + ".mp4")
    except:
        print("Connection Error")  # to handle exception 

    return video_name + ".mp4"


def video_info(video_filepath):
    """ this function returns number of channels, bit rate, and sample rate of the video"""

    video_data = mediainfo(video_filepath)
    print(video_data)
    channels = video_data["channels"]
    bit_rate = video_data["bit_rate"]
    sample_rate = video_data["sample_rate"]

    return channels, bit_rate, sample_rate


def video_to_audio(video_filepath, filename, video_channels, video_bit_rate, video_sample_rate):
    audio_filename = filename + ".wav"
    command = f"ffmpeg -y -i {video_filepath} -b:a {video_bit_rate} -ac {video_channels} -ar {video_sample_rate} -vn {audio_filename}"
    subprocess.call(command, shell=True)
    blob_name = f"audios/{LECTURE_ID}/{audio_filename}"
    upload_blob(BUCKET_NAME, audio_filename, blob_name)
    return blob_name


def long_running_recognize(storage_uri, channels, sample_rate):
    client = speech_v1.SpeechClient()

    config = {
        "language_code": "tr-TR",
        "sample_rate_hertz": int(sample_rate),
        "encoding": enums.RecognitionConfig.AudioEncoding.LINEAR16,
        "audio_channel_count": int(channels),
        "enable_word_time_offsets": True,
        "model": "default",
        "enable_automatic_punctuation": True
    }
    audio = {"uri": storage_uri}

    operation = client.long_running_recognize(config, audio)

    print(u"Waiting for operation to complete...")
    response = operation.result()
    return response


def subtitle_generation(speech_to_text_response, bin_size=3):
    """We define a bin of time period to display the words in sync with audio. 
    Here, bin_size = 3 means each bin is of 3 secs. 
    All the words in the interval of 3 secs in result will be grouped togather."""
    transcriptions = []
    index = 0

    for result in response.results:
        try:
            if result.alternatives[0].words[0].start_time.seconds:
                # bin start -> for first word of result
                start_sec = result.alternatives[0].words[0].start_time.seconds
                start_microsec = result.alternatives[0].words[0].start_time.nanos * 0.001
            else:
                # bin start -> For First word of response
                start_sec = 0
                start_microsec = 0
            end_sec = start_sec + bin_size  # bin end sec

            # for last word of result
            last_word_end_sec = result.alternatives[0].words[-1].end_time.seconds
            last_word_end_microsec = result.alternatives[0].words[-1].end_time.nanos * 0.001

            # bin transcript
            transcript = result.alternatives[0].words[0].word

            index += 1  # subtitle index

            for i in range(len(result.alternatives[0].words) - 1):
                try:
                    word = result.alternatives[0].words[i + 1].word
                    word_start_sec = result.alternatives[0].words[i + 1].start_time.seconds
                    word_start_microsec = result.alternatives[0].words[
                                              i + 1].start_time.nanos * 0.001  # 0.001 to convert nana -> micro
                    word_end_sec = result.alternatives[0].words[i + 1].end_time.seconds
                    word_end_microsec = result.alternatives[0].words[i + 1].end_time.nanos * 0.001

                    if word_end_sec < end_sec:
                        transcript = transcript + " " + word
                    else:
                        previous_word_end_sec = result.alternatives[0].words[i].end_time.seconds
                        previous_word_end_microsec = result.alternatives[0].words[i].end_time.nanos * 0.001

                        # append bin transcript
                        transcriptions.append(srt.Subtitle(index, datetime.timedelta(0, start_sec, start_microsec),
                                                           datetime.timedelta(0, previous_word_end_sec,
                                                                              previous_word_end_microsec), transcript))

                        # reset bin parameters
                        start_sec = word_start_sec
                        start_microsec = word_start_microsec
                        end_sec = start_sec + bin_size
                        transcript = result.alternatives[0].words[i + 1].word

                        index += 1
                except IndexError:
                    pass
            # append transcript of last transcript in bin
            transcriptions.append(srt.Subtitle(index, datetime.timedelta(0, start_sec, start_microsec),
                                               datetime.timedelta(0, last_word_end_sec, last_word_end_microsec),
                                               transcript))
            index += 1
        except IndexError:
            pass

    # turn transcription list into subtitles
    subtitles = srt.compose(transcriptions)
    return subtitles


conn = psycopg2.connect(
    host="localhost",
    database="postgres",
    user="postgres",
    password="postgres")
cursor = conn.cursor()
cursor.execute(f"""select ls.t_name, vf.raw_full_path from content.lecture as l
inner join content.lecture_chapter as lc on lc.fk_lecture_id = l.pk_lecture_id
inner join content.lecture_subject as ls on ls.fk_lecture_chapter_id = lc.pk_lecture_chapter_id
inner join asset.video_file as vf on vf.pk_video_file_guid = ls.fk_video_file_guid
where l.pk_lecture_id = {LECTURE_ID} order by lc.list_order, ls.list_order ;""")
rows = cursor.fetchall()
for row in rows:
    video_name = row[0].replace(' ', '_')
    video_path = row[1]
    subtitle_fname = f"{video_name}_subtitle.srt"
    if os.path.isfile(subtitle_fname):
        continue
    print(video_name)
    print(video_path)
    video_path = download_video(FTP_BASE_URL + video_path, video_name)
    channels, bit_rate, sample_rate = video_info(video_path)
    blob_name = video_to_audio(video_path, video_name, channels, bit_rate, sample_rate)
    gcs_uri = f"gs://{BUCKET_NAME}/{blob_name}"
    response = long_running_recognize(gcs_uri, channels, sample_rate)
    subtitles = subtitle_generation(response)
    with open(subtitle_fname, "w") as f:
        f.write(subtitles)
