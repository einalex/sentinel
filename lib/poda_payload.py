import os
import sys
import syscoinlib
import boto3
import botocore
from misc import printdbg
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'lib'))
from bitcoinrpc.authproxy import JSONRPCException

class PoDAPayload():
    bucketname = 'poda'
    def __init__(self, accountid, keyid, secret):
        self.connect_db(accountid, keyid, secret)

    @classmethod
    def connect_db(self, accountid, keyid, secret):
        if accountid != '' and keyid != '' and secret != '':
            self.s3 = boto3.resource('s3',
                endpoint_url = 'https://{0}.r2.cloudflarestorage.com'.format(accountid),
                aws_access_key_id = keyid,
                aws_secret_access_key = secret,
            )

    @classmethod
    def get_local_block_processed(self, vh):
        import peewee
        from models import Setting
        try:
            Setting.get(Setting.name == vh)
        except (peewee.OperationalError, peewee.DoesNotExist, peewee.ProgrammingError):
            printdbg("[info]: Can't get local vh...")
            return False
        return True

    @classmethod
    def set_local_block_processed(self, blockhash):
        from models import Setting
        Setting.get_or_create(name=blockhash)

    @classmethod
    def get_last_block(self):
        import peewee
        from models import Setting
        lastblock = ''
        try:
            lastblock = Setting.get(Setting.name == 'lastpodablock').value
        except (peewee.OperationalError, peewee.DoesNotExist, peewee.ProgrammingError):
            printdbg("[info]: Can't get lastpodablock...")
        return lastblock

    @classmethod
    def set_last_block(self, lastblockIn):
        from models import Setting
        lastblock_setting, created = Setting.get_or_create(name='lastpodablock')
        lastblock_setting.value = lastblockIn
        lastblock_setting.save()

    @classmethod
    def send_blobs(self, syscoind):
        # get last processed block from gateway
        lastblockhash = self.get_last_block()
        # get prevCL info
        mediantimePrevCl = 0
        try:
            cl = syscoind.rpc_command('getchainlocks')
            if cl is not None:
                prevCL = cl.get('previous_chainlock')
                if prevCL is not None:
                    mediantimePrevCl = syscoind.rpc_command('getblock', prevCL.get('blockhash')).get('mediantime')
        except JSONRPCException as e:
            print("Unable to fetch prev CL: %s" % e.message)
            mediantimePrevCl = 0
        # loop through tip to last block or 7 hours back from prevCL or tip
        try:
            latestHash = syscoind.rpc_command('getbestblockhash')
            latestBlock = syscoind.rpc_command('getblock', latestHash)
            medianTimeTip = latestBlock.get('mediantime')
            mediantime = medianTimeTip
            # loop over 7 hours from tip or until lastblock_height whichever is first
            while True:
                # if prevCL - 7 hours for this block or if no prevCL then 7 hours from tip or if gateway's last block then break
                if mediantimePrevCl > 0 and (mediantimePrevCl - mediantime) > 7*60*60:
                    print("Time traversed back over 7 hours from mediantimePrevCl: %d" % mediantimePrevCl)
                    break
                elif mediantimePrevCl == 0 and (medianTimeTip - mediantime) > 7*60*60:
                    print("Time traversed back over 7 hours from medianTimeTip: %d" % medianTimeTip)
                    break
                if latestBlock.get('hash') == lastblockhash:
                    print("Found last block hash during traversal: %s" % lastblockhash)
                    break
                # only process blocks that have not been processed already
                if self.get_local_block_processed(latestHash) is False:
                    # get txids and check PoDA
                    items = latestBlock.get('tx')
                    for txid in items:
                        try:
                            blobresponse = syscoind.rpc_command('getnevmblobdata', txid, True)  
                            try:
                                print("checking PoDA txid {0} {1}".format(txid, self.bucketname))
                                self.s3.Object(self.bucketname, blobresponse.get('versionhash')).load()
                            except botocore.exceptions.ClientError as e:
                                if e.response['Error']['Code'] == "404":
                                    print("Found PoDA txid! storing in db: %s" % blobresponse.get('versionhash'))
                                    # send to DB backend
                                    object = self.s3.Object(self.bucketname, blobresponse.get('versionhash'))
                                    result = object.put(Body=blobresponse.get('data'))
                                    res = result.get('ResponseMetadata')
                                    if res.get('HTTPStatusCode') != 200:
                                        print('Blob Not Uploaded')
                                        return
                                    pass
                                else:
                                    # Something else has gone wrong.
                                    print("Unable to check for vh existance from backend: %s" % e.message)
                                    raise
                        except JSONRPCException:
                            continue
                # used to check against last cached block to know when to stop processing
                latestBlock = syscoind.rpc_command('getblock', latestBlock.get('previousblockhash'))
                # need to be able to detect MTP is 7 hours old from tip to know when to stop processing
                mediantime = latestBlock.get('mediantime')
        except JSONRPCException as e:
            print("Unable to fetch latest block: %s" % e.message)
        # processed block and stored in DB so set it in cache so we can continue on from here on subsequent cycles
        self.set_last_block(latestHash)
        self.set_local_block_processed(latestHash)

    @classmethod
    def get_data(self, vh):
        obj = ''
        try:
            obj = self.s3.Object(self.bucketname, vh).get()
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == "404" or e.response['Error']['Code'] == 'NoSuchKey':
                printdbg("Data does not exist for vh: %s" % vh)
                return ''
            else:
                # Something else has gone wrong.
                raise
        # The object does exist.
        return obj['Body'].read().decode('utf-8')