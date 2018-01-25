import boto3
import semver
from aws_conduit import conduit_factory as factory
from aws_conduit.conduit_portfolio import ConduitPortfolio

SESSION = boto3.session.Session()
IAM = boto3.client('iam')
STS = boto3.client('sts')

CONFIG_PREFIX = 'conduit.yaml'

RESOURCES_KEY = "__resources__"
BUCKET_KEY = "__bucket__"
PREFIX_KEY = "__prefix__"


def get_region():
    region = SESSION.region_name
    return region


def get_account_id():
    account_id = STS.get_caller_identity().get('Account')
    return account_id


def get_alias():
    """
    Get the users account alias.

    Return:
        alias: The first known account alias.
    """
    aliases = IAM.list_account_aliases()
    if aliases and aliases['AccountAliases']:
        return aliases['AccountAliases'][0]


def get_portfolio(config, name=None, portfolio_id=None):
    for portfolio in config['portfolios']:
        if name is not None:
            if portfolio.name == name:
                return portfolio
        if portfolio_id is not None:
            if portfolio.portfolio_id == portfolio_id:
                return portfolio
    raise ValueError('Portfolio not found: {} {}'.format(portfolio_id, name))


def get_product(config, name=None, product_id=None):
    for portfolio in config['portfolios']:
        for product in portfolio.products:
            if product_id is not None:
                if product.product_id == product_id:
                    return product
            if name is not None:
                if product.name == name:
                    return product
    raise ValueError('Product not found: {} {}'.format(product_id, name))


ACCOUNT_ID = get_account_id()


def inject_config(function):

    start = factory.start()
    bucket = start.create_s3()
    configuration = bucket.get_config(CONFIG_PREFIX)

    def wrapper(*args, **kwargs):
        result = function(*args, **kwargs, config=configuration)
        bucket.put_config(configuration, CONFIG_PREFIX)
        return result

    return wrapper


def find_build_product(spec, config):
    portfolio = None
    product = None
    for port in config['portfolios']:
        if isinstance(port, ConduitPortfolio):
            if port.name == spec['portfolio']:
                portfolio = port
                for prod in portfolio.products:
                    if prod.name == spec['product']:
                        product = prod
                        break
        else:
            if port['name'] == spec['portfolio']:
                portfolio = port
                for prod in portfolio['products']:
                    if prod['name'] == spec['product']:
                        product = prod
                        break

    return dict(
        product=product,
        portfolio=portfolio
    )


def get_all_portfolio_artifacts(portfolio_name, config):
    templates = []
    for port in config['portfolios']:
        if isinstance(port, ConduitPortfolio):
            if port.name == portfolio_name:
                for product in port.products:
                    templates.append(dict(
                        template=product.template_location,
                        product=product.name
                    ))
        else:
            if port['name'] == portfolio_name:
                for product in port['products']:
                    item = dict(
                        template=product['template_location'],
                        product=product['name'],
                        version=product['nextVersion']
                    )
                    if 'resources' in product:
                        item['resources'] = product['resources']
                    if 'nestedStacks' in product:
                        item['nestedStacks'] = product['nestedStacks']
                    templates.append(item)
    return templates


def find_provisioned_build_product(to_find_product, spec, config):
    portfolio = None
    product = None
    for port in config['portfolios']:
        if port.name == spec['portfolio']:
            portfolio = port
            for prod in portfolio.products:
                if hasattr(prod, provisioned):
                    for prov in prod.provisioned:
                        if prov == to_find_product:
                            product = prod
                            break

    if portfolio is None or not portfolio.exists():
        raise ValueError("The specified portfolio does not exist: {}".format(to_find_product))
    elif product is None or not product.exists():
        raise ValueError("The product {} does not exist in portfolio {}".format(to_find_product, spec['portfolio']))
    return product


def find_s3_build_product(spec, config):
    default_product = dict(
        name=spec['product'],
        currentVersion='0.0.0',
        nextVersion='0.0.0'
    )
    result = find_build_product(spec, config)
    if result['portfolio'] is None:
        result['portfolio'] = dict(
            name=spec['portfolio'],
            products=[default_product]
        )
        result['product'] = default_product
        config['portfolios'].append(result['portfolio'])
    elif result['product'] is None:
        result['product'] = default_product
        result['portfolio']['products'].append(default_product)

    return result


def next_version(release_type, current_version):
    product_version = current_version
    if release_type == 'build':
        product_version = semver.bump_build(current_version)
    if release_type == 'major':
        product_version = semver.bump_major(current_version)
    if release_type == 'minor':
        product_version = semver.bump_minor(current_version)
    if release_type == 'patch':
        product_version = semver.bump_patch(current_version)
    return product_version


def put_resource(path, bucket, portfolio, product, version, environment='core'):
    if environment is not None:
        key = "{}/{}/{}/{}/{}".format(portfolio, product, environment, version, path)
        prefix = "{}/{}/{}/{}".format(portfolio, product, environment, version)
        directory = "{}/{}/{}/{}/{}".format(bucket.name, portfolio, product, environment, version)
    else:
        key = "{}/{}/{}/{}".format(portfolio, product, version, path)
        prefix = "{}/{}/{}".format(portfolio, product, version)
        directory = "{}/{}/{}/{}".format(bucket.name, portfolio, product, version)
    print("Adding resource to release: {}".format(path))
    replace_resources(directory, bucket, prefix, path=path)
    bucket.put_resource(path, key)
    revert_resources(directory, path=path)
    return "https://s3-{}.amazonaws.com/{}/{}".format(get_region(), directory, path)


def read_write(function):

    def wrapper(*args, **kwargs):
        if 'path' in kwargs:
            if kwargs['path'].endswith('yaml') or kwargs['path'].endswith('yml') or kwargs['path'].endswith('json'):
                f = open(kwargs['path'], 'r')
                filedata = f.read()
                f.close()
                newdata = function(*args, **kwargs, file_data=filedata)
                if newdata is not None:
                    f = open(kwargs['path'], 'w')
                    f.write(newdata)
                    f.close()
    return wrapper


@read_write
def replace_resources(directory, bucket, prefix, path=None, file_data=None):
    if file_data is not None:
        print("Replacing in {}".format(path))
        data = file_data.replace(RESOURCES_KEY, directory)
        data = data.replace(BUCKET_KEY, bucket.name)
        data = data.replace(PREFIX_KEY, prefix)
        return data


@read_write
def revert_resources(directory, path=None, file_data=None):
    if file_data is not None:
        print("Replacing in {}".format(path))
        return file_data.replace(directory, RESOURCES_KEY)


def put_sls_resource(path, bucket, portfolio, product, version, sls_package, environment='core'):
    new_path = path
    if '.serverless' in new_path:
        new_path = new_path.replace('.serverless/', '')
    directory = "{}/{}/{}/{}".format(portfolio, product, environment, version)
    key = "{}/{}/{}/{}/{}".format(portfolio, product, environment, version, new_path)
    replace_sls_resources(directory, bucket.name, sls_package, environment, path=path)
    print("Adding sls resource to release: {}".format(path))
    bucket.put_resource(path, key)
    revert_sls_resources(directory, bucket.name, sls_package, environment, path=path)
    return "https://s3-{}.amazonaws.com/{}/{}/{}".format(get_region(), bucket.name, directory, new_path)


@read_write
def replace_sls_resources(key, bucket, sls_package, environment, path=None, file_data=None):
    if file_data is not None:
        print("Replacing in {}".format(path))
        print("The key is: {}".format(key))
        return file_data.replace(sls_package['artifactDirectoryName'], key).replace(sls_package['bucket'], bucket).replace('${STAGE}', environment)


@read_write
def revert_sls_resources(key, bucket, sls_package, environment, path=None, file_data=None):
    if file_data is not None:
        print("Reverting in {}".format(path))
        print("The key is: {}".format(key))
        return file_data.replace(key, sls_package['artifactDirectoryName']).replace(bucket, sls_package['bucket']).replace(environment, '${STAGE}')
