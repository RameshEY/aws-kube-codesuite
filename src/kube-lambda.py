import yaml, boto3, botocore, json, zipfile
from os import path
from kubernetes import client, config
from kubernetes.stream import stream
from time import sleep

s3 = boto3.resource('s3')
code_pipeline = boto3.client('codepipeline')
ssm = boto3.client('ssm')


class PXVolume:

    def __init__(self, row):
        split_row = row.split('\t')
        self.id = split_row[0]
        self.name = split_row[1]


def deploy_stage(event, context):
    build_json = download_build_json(event=event)
    download_templates(build_json=build_json)
    k8s_beta, k8s_core = kubernetes_auth(build_json=build_json)
    code_pipeline_job_id = event['CodePipeline.job']['id']

    # Update deployment template
    inplace_change("/tmp/stage-deployment-template.yml", "$REPOSITORY_URI", build_json["repository-uri"])
    inplace_change("/tmp/stage-deployment-template.yml", "$TAG", build_json["tag"])
    with open(path.join(path.dirname(__file__), "/tmp/stage-deployment-template.yml")) as f:
        deployment = yaml.load(f)
    stage_namespace = deployment["metadata"]["namespace"]

    with FailPipelineOnException(code_pipeline_job_id=code_pipeline_job_id):
        # Old DB pod template
        with open(path.join(path.dirname(__file__), "/tmp/stage-db-template.yml")) as f:
            db_pod = yaml.load(f)

        # Delete current stage pod
        pod_name = db_pod["metadata"]["name"]
        k8s_core.delete_namespaced_pod(name=pod_name,
                                       body=db_pod,
                                       namespace=stage_namespace)
        snap_volume_id = snap_volume(k8s_core)

        # Update db template
        inplace_change("/tmp/stage-db-template.yml", "$VOLUME_ID", snap_volume_id)
        with open(path.join(path.dirname(__file__), "/tmp/stage-db-template.yml")) as f:
            db_pod = yaml.load(f)

        # Deploy database pod mounted to the snapshot volume
        # Sometimes the new pod can't be recreated right away, so
        # retry several times before failing
        attempts = 10
        sleep_between_attempts = 5
        for i in range(attempts):
            try:
                k8s_core.create_namespaced_pod(name=pod_name,
                                               body=db_pod,
                                               namespace=stage_namespace)
            except Exception as e:
                if i == attempts-1:
                    raise e
                else:
                    sleep(sleep_between_attempts)
            else:
                break

        # Update application deployment
        resp = k8s_beta.patch_namespaced_deployment(name=deployment["metadata"]["name"],
                                                    body=deployment,
                                                    namespace=["metadata"]["namespace"])
        print("Deployment created. status='%s'" % str(resp.status))

    code_pipeline.put_job_success_result(jobId=code_pipeline_job_id)
    return 'Success'


def deploy_prod(event, context):
    build_json = download_build_json(event=event)
    download_templates(build_json=build_json)
    k8s_beta, k8s_core = kubernetes_auth(build_json=build_json)
    code_pipeline_job_id = event['CodePipeline.job']['id']

    # Update deployment template
    inplace_change("/tmp/prod-deployment-template.yml", "$REPOSITORY_URI", build_json["repository-uri"])
    inplace_change("/tmp/prod-deployment-template.yml", "$TAG", build_json["tag"])
    with open(path.join(path.dirname(__file__), "/tmp/prod-deployment-template.yml")) as f:
        deployment = yaml.load(f)

    # Update deployment or fail
    with FailPipelineOnException(code_pipeline_job_id=code_pipeline_job_id):
        resp = k8s_beta.patch_namespaced_deployment(name=deployment["metadata"]["name"],
                                                    body=deployment,
                                                    namespace=["metadata"]["namespace"])
        print("Deployment created. status='%s'" % str(resp.status))

    code_pipeline.put_job_success_result(jobId=code_pipeline_job_id)
    return 'Success'


def download_templates(build_json):
    """
    Download templates from `kube-manifests` directory for local use

    :param build_json: Build step artifact data
    """
    s3.meta.client.download_file(build_json["template-bucket"], 'config', '/tmp/config')
    s3.meta.client.download_file(build_json["template-bucket"], 'prod-deployment-template.yml', '/tmp/prod-deployment-template.yml')
    s3.meta.client.download_file(build_json["template-bucket"], 'stage-deployment-template.yml', '/tmp/stage-deployment-template.yml')
    s3.meta.client.download_file(build_json["template-bucket"], 'stage-db-template.yml', '/tmp/stage-db-template.yml')


def download_build_json(event):
    """
    Download the artifact from the Build step which has
    useful parameters for later

    :param event: Lambda event this function was called with
    :return: Dictionary of the Build step artifact's data
    """
    cplKey = event['CodePipeline.job']['data']['inputArtifacts'][0]['location']['s3Location']['objectKey']
    cplBucket = event['CodePipeline.job']['data']['inputArtifacts'][0]['location']['s3Location']['bucketName']

    s3.meta.client.download_file(cplBucket,cplKey,'/tmp/build.zip')

    zip_ref = zipfile.ZipFile('/tmp/build.zip', 'r')
    zip_ref.extractall('/tmp/')
    zip_ref.close()

    with open('/tmp/build.json') as json_data:
        return json.load(json_data)


def kubernetes_auth(build_json):
    """
    Build config file from template and secrets in SSM and use
    it to authenticate with Kubernetes cluster

    :param build_json: Configuration from Build step
    :return: Handles to Kubernetes BetaAPI, CoreAPI
    """
    CA = ssm.get_parameter(Name='CA', WithDecryption=True)["Parameter"]["Value"]
    CLIENT_CERT = ssm.get_parameter(Name='ClientCert', WithDecryption=True)["Parameter"]["Value"]
    CLIENT_KEY = ssm.get_parameter(Name='ClientKey', WithDecryption=True)["Parameter"]["Value"]

    inplace_change("/tmp/config", "$ENDPOINT", build_json["cluster-endpoint"])
    inplace_change("/tmp/config", "$CA", CA)
    inplace_change("/tmp/config", "$CLIENT_CERT", CLIENT_CERT)
    inplace_change("/tmp/config", "$CLIENT_KEY", CLIENT_KEY)

    config.load_kube_config('/tmp/config')
    k8s_beta = client.ExtensionsV1beta1Api()
    k8s_core = client.CoreV1Api()
    return k8s_beta, k8s_core


def inplace_change(filename, old_string, new_string):
    """
    Replace all instances of `old_string` with `new_string`
    in given `filename`

    :param filename: File to replace contents
    :param old_string: String to be replaced
    :param new_string: String to replace `old_string` with
    """
    with open(filename) as f:
        s = f.read()
        if old_string not in s:
            # print '"{old_string}" not found in {filename}.'.format(**locals())
            return

    with open(filename, 'w') as f:
        # print 'Changing "{old_string}" to "{new_string}" in {filename}'.format(**locals())
        s = s.replace(old_string, new_string)
        f.write(s)


def snap_volume(k8s_core):
    """
    Take a snapshot of prod volume and return snap's VolumeID

    :return: Volume ID
    """

    def pxctl(command):
        return stream(k8s_core.connect_get_namespaced_pod_exec,
                      name=pwx_pod_name,
                      namespace="kube-system",
                      command=['/opt/pwx/bin/pxctl'] + command,
                      stdin=False, stdout=True,
                      stderr=False, tty=False).strip().split('\n')[1:]

    pods = k8s_core.list_namespaced_pod(namespace="kube-system")
    pwx_pod_name = [pod.metadata.name for pod in pods.items if 'portworx' in pod.metadata.name][0]
    pvc_id = k8s_core.list_namespaced_persistent_volume_claim(namespace='demo-app-prod').items[0].spec.volume_name

    # Get prod's volume ID
    px_volumes = [PXVolume(row) for row in pxctl([
        'volume',
        'list'
    ])]
    prod_volume_id = [volume for volume in px_volumes if volume.name == pvc_id][0].id

    # Delete old staging snaps
    px_snaps = [PXVolume(row) for row in pxctl([
        'snap',
        'list',
        '-l',
        'env=stage'
    ])]
    for snap in px_snaps:
        pxctl([
            'snap',
            'delete',
            snap.id
        ])

    # Snap!
    pxctl([
        'snap',
        'create',
        prod_volume_id,
        '-l',
        'env=stage'
    ])

    return [PXVolume(row) for row in pxctl([
        'snap',
        'list',
        '-l',
        'env=stage'
    ])][0].id


class FailPipelineOnException:
    """
    Context manager. Any uncaught exceptions fail the pipeline
    """
    def __init__(self, code_pipeline_job_id):
        self.code_pipeline_job_id = code_pipeline_job_id

    def __enter__(self):
        pass

    def __exit__(self, type, value, traceback):
        if traceback is not None:
            failure_message = "Job failed: {}".format(value)
            code_pipeline.put_job_failure_result(jobId=self.code_pipeline_job_id,
                                                 failureDetails={
                                                     'message': failure_message,
                                                     'type': 'JobFailed'
                                                 })
            print(failure_message)
            # This will cause the exception to re-raise
            return False
